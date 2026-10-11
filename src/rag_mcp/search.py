"""Hybrid retrieval: vector similarity + keyword match, fused with Reciprocal Rank Fusion.

Why both: questions about a parameter or limit hinge on exact tokens
(`max_connections`, `tcp_keepalives_idle`) where embeddings are fuzzy, while
paraphrased questions need semantics where keywords fail. RRF combines the two
rankings without having to calibrate cosine distances against keyword scores.

Keyword side, two deliberate choices:
  * Query: Postgres' websearch_to_tsquery ANDs every word ("how & many & can"),
    so natural-language questions would almost never match. We drop stopwords
    and match each remaining term on its own; identifiers the parser splits
    (max_connections -> max, connections) are matched as exact phrases.
  * Ranking: ts_rank has no notion of term rarity, so pages repeating "Aiven
    PostgreSQL" outranked the one chunk containing `tcp_keepalives_idle`. A chunk
    scores the sum of BM25-style IDF weights, log(1 + (N - df + .5) / (df + .5)),
    of the terms it contains: rare terms dominate, common ones barely count.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from psycopg_pool import ConnectionPool

from rag_mcp.embedder import Embedder

CANDIDATES_PER_RETRIEVER = 30  # each side's top-N fed into fusion
RRF_K = 60  # standard RRF constant: dampens the weight of the very top ranks
MAX_KEYWORD_TERMS = 12

_STOPWORDS = frozenset("""
a about above after again all also am an and any are as at be because been before being below
between both but by can could did do does doing down during each few for from further had has have
having he her here hers him his how i if in into is it its itself just me more most my no nor not
now of off on once only or other our out over own same she should so some such than that the their
them then there these they this those through to too under until up very was we were what when
where which while who whom why will with would you your yours
""".split())
_TERM = re.compile(r"[A-Za-z0-9][\w.\-/]*[A-Za-z0-9]|[A-Za-z0-9]")


@dataclass(frozen=True)
class Hit:
    rank: int
    score: float
    vector_rank: int | None
    keyword_rank: int | None
    source_uri: str
    title: str | None
    heading_path: list[str]
    content: str


def keyword_terms(query: str) -> list[str]:
    """Search terms for the keyword side: lowercase, deduplicated, no stopwords."""
    seen: list[str] = []
    for term in _TERM.findall(query.lower()):
        if term not in _STOPWORDS and term not in seen and (len(term) > 1 or term.isdigit()):
            seen.append(term)
    return seen[:MAX_KEYWORD_TERMS]


_SQL = """
WITH vector_hits AS (
    -- Top-N in a subquery first, so ORDER BY distance LIMIT N can use the HNSW index;
    -- a window function around it would force computing the distance for every row.
    SELECT id, row_number() OVER (ORDER BY dist) AS rnk FROM (
        SELECT c.id, c.embedding <=> %(qvec)s AS dist
        FROM document_chunks c
        WHERE %(filter)s::text IS NULL OR c.source_id IN (
            SELECT id FROM sources WHERE strpos(lower(source_uri), lower(%(filter)s)) > 0)
        ORDER BY c.embedding <=> %(qvec)s
        LIMIT %(n)s
    ) v
),
terms AS (
    SELECT phraseto_tsquery('simple', term) AS q
    FROM unnest(%(terms)s::text[]) AS term
),
term_weights AS (
    -- Document frequency per term via the GIN index, turned into a BM25 IDF weight.
    SELECT t.q, ln(1 + (n.total - df.cnt + 0.5) / (df.cnt + 0.5)) AS idf
    FROM terms t
    CROSS JOIN (SELECT count(*)::float AS total FROM document_chunks) n
    CROSS JOIN LATERAL (SELECT count(*) AS cnt FROM document_chunks c WHERE c.content_tsv @@ t.q) df
),
keyword_hits AS (
    SELECT id, row_number() OVER (ORDER BY kw_score DESC, id) AS rnk FROM (
        SELECT c.id, sum(w.idf) AS kw_score
        FROM document_chunks c
        JOIN term_weights w ON c.content_tsv @@ w.q
        WHERE %(filter)s::text IS NULL OR c.source_id IN (
            SELECT id FROM sources WHERE strpos(lower(source_uri), lower(%(filter)s)) > 0)
        GROUP BY c.id
        ORDER BY kw_score DESC, c.id
        LIMIT %(n)s
    ) k
),
fused AS (
    SELECT id,
           sum(1.0 / (%(rrf_k)s + rnk)) AS score,
           min(rnk) FILTER (WHERE side = 'v') AS vector_rank,
           min(rnk) FILTER (WHERE side = 'k') AS keyword_rank
    FROM (SELECT id, rnk, 'v' AS side FROM vector_hits
          UNION ALL
          SELECT id, rnk, 'k' AS side FROM keyword_hits) both_sides
    GROUP BY id
)
SELECT f.score, f.vector_rank, f.keyword_rank, s.source_uri, s.title, c.heading_path, c.content
FROM fused f
JOIN document_chunks c ON c.id = f.id
JOIN sources s ON s.id = c.source_id
ORDER BY f.score DESC, c.id
LIMIT %(k)s
"""


class Searcher:
    def __init__(self, pool: ConnectionPool, embedder: Embedder) -> None:
        self.pool = pool
        self.embedder = embedder

    def search(self, query: str, top_k: int = 5, source_filter: str | None = None) -> list[Hit]:
        qvec = self.embedder.embed_query(query)
        params = {
            "qvec": qvec, "terms": keyword_terms(query), "filter": source_filter or None,
            "n": CANDIDATES_PER_RETRIEVER, "rrf_k": RRF_K, "k": top_k,
        }
        with self.pool.connection() as conn, conn.transaction():
            if source_filter:
                # With a filter, a plain HNSW scan can run out of candidates before LIMIT;
                # iterative scan (pgvector >= 0.8) keeps searching the graph.
                conn.execute("SET LOCAL hnsw.iterative_scan = relaxed_order")
            rows = conn.execute(_SQL, params).fetchall()
        return [
            Hit(rank=i + 1, score=float(score), vector_rank=vr, keyword_rank=kr,
                source_uri=uri, title=title, heading_path=list(path), content=content)
            for i, (score, vr, kr, uri, title, path, content) in enumerate(rows)
        ]


def format_hits(query: str, hits: list[Hit]) -> str:
    """Markdown for Claude: one block per hit with breadcrumb, citation link and scores."""
    if not hits:
        return (f"No results for {query!r}. Try different wording, remove the source filter, "
                "or check list_sources to see what has been ingested.")
    blocks = []
    for h in hits:
        crumb = " > ".join(h.heading_path) or h.title or h.source_uri
        sides = [f"vector #{h.vector_rank}" if h.vector_rank else "",
                 f"keyword #{h.keyword_rank}" if h.keyword_rank else ""]
        blocks.append(
            f"[{h.rank}] {crumb}\n"
            f"source: {h.source_uri}\n"
            f"score: {h.score:.4f} ({', '.join(s for s in sides if s)})\n\n"
            f"{h.content.strip()}"
        )
    return f"{len(hits)} results for {query!r}:\n\n" + "\n\n---\n\n".join(blocks)
