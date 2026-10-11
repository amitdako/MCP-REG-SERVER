"""Load benchmark: real measurements for the "Load" and "Where it breaks" slides.

Usage (inside the image, devportal already ingested into the main DB):
    python scripts/benchmark.py latency       # sequential search latency, embed vs SQL split
    python scripts/benchmark.py concurrency   # 1..16 parallel searchers: throughput, latency
    python scripts/benchmark.py ingest        # big single document; search latency during ingest
    python scripts/benchmark.py scale         # corpus x1 / x10 / x50 in a separate DB

`ingest` and `scale` write only to a separate database (rag_bench); the main
database is only read.
"""

from __future__ import annotations

import json
import statistics
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import psycopg
from psycopg import sql

from rag_mcp import db, loaders
from rag_mcp.config import Settings
from rag_mcp.embedder import SentenceTransformerEmbedder
from rag_mcp.ingest import Ingestor
from rag_mcp.search import CANDIDATES_PER_RETRIEVER, Searcher, keyword_terms

QUESTIONS = [
    json.loads(line)["question"]
    for line in (Path(__file__).resolve().parent.parent / "eval" / "questions.jsonl").read_text("utf-8").splitlines()
    if line.strip()
]
SETTINGS = Settings.from_env()
BENCH_DB = "rag_bench"


def pct(values: list[float], p: float) -> float:
    return float(np.percentile(values, p))


def describe(ms: list[float]) -> str:
    return f"p50 {pct(ms, 50):.0f} ms, p95 {pct(ms, 95):.0f} ms, p99 {pct(ms, 99):.0f} ms, max {max(ms):.0f} ms (n={len(ms)})"


def bench_url() -> str:
    return psycopg.conninfo.make_conninfo(SETTINGS.database_url, dbname=BENCH_DB)


def reset_bench_db() -> str:
    admin = psycopg.conninfo.make_conninfo(SETTINGS.database_url, dbname="postgres")
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(BENCH_DB)))
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(BENCH_DB)))
    db.ensure_schema(bench_url(), SETTINGS.embedding_dim)
    return bench_url()


def new_embedder() -> SentenceTransformerEmbedder:
    e = SentenceTransformerEmbedder(SETTINGS.embedding_model, SETTINGS.embedding_dim,
                                    batch_size=SETTINGS.ingest_batch_size)
    e.embed_query("warm up")
    return e


# ---------------------------------------------------------------- latency

def latency(passes: int = 3) -> None:
    embedder = new_embedder()
    with db.create_pool(SETTINGS.database_url) as pool:
        searcher = Searcher(pool, embedder)
        searcher.search("warm up")
        embed_ms, sql_ms, total_ms = [], [], []
        for _ in range(passes):
            for q in QUESTIONS:
                t0 = time.perf_counter()
                qvec = embedder.embed_query(q)
                t1 = time.perf_counter()
                searcher.search_with_vector(qvec, q)
                t2 = time.perf_counter()
                embed_ms.append((t1 - t0) * 1000)
                sql_ms.append((t2 - t1) * 1000)
                total_ms.append((t2 - t0) * 1000)
    print("## Sequential search latency (warm, top_k=5, real corpus)")
    print(f"total : {describe(total_ms)}")
    print(f"embed : {describe(embed_ms)}")
    print(f"SQL   : {describe(sql_ms)}")


# ---------------------------------------------------------------- concurrency

def concurrency(levels=(1, 2, 4, 8, 16), per_level: int = 60) -> None:
    """Parallel searchers sharing one server process (one model, a 4-connection pool),
    i.e. what one server instance can serve."""
    embedder = new_embedder()
    print("## Concurrent searches against one server process")
    print("| parallel clients | throughput | p50 | p95 | max |")
    print("|---|---|---|---|---|")
    with db.create_pool(SETTINGS.database_url) as pool:
        searcher = Searcher(pool, embedder)
        searcher.search("warm up")
        for n in levels:
            latencies: list[float] = []

            def one(i: int) -> None:
                t = time.perf_counter()
                searcher.search(QUESTIONS[i % len(QUESTIONS)])
                latencies.append((time.perf_counter() - t) * 1000)

            started = time.perf_counter()
            with ThreadPoolExecutor(max_workers=n) as ex:
                list(ex.map(one, range(per_level)))
            elapsed = time.perf_counter() - started
            print(f"| {n} | {per_level / elapsed:.1f} req/s | {pct(latencies, 50):.0f} ms | "
                  f"{pct(latencies, 95):.0f} ms | {max(latencies):.0f} ms |")


# ---------------------------------------------------------------- ingest

def synthetic_markdown(target_bytes: int) -> str:
    """Realistic-looking docs text: real devportal-style sentences with numbered sections."""
    para = ("To configure the service, open the Aiven Console, select your project and service, "
            "then set the advanced parameter to the required value and save the changes. ")
    parts, size, i = [], 0, 0
    while size < target_bytes:
        block = f"## Section {i}\n\n" + (para * 4) + f"Setting value_{i} defaults to {i * 10}.\n\n"
        parts.append(block)
        size += len(block)
        i += 1
    return "# Synthetic large guide\n\n" + "".join(parts)


def ingest() -> None:
    url = reset_bench_db()
    embedder = new_embedder()
    with db.create_pool(url) as pool:
        ingestor = Ingestor(pool, embedder)
        print("## Single-document ingest (synchronous `ingest` tool path)")
        for size_kb in (10, 100, 1000):
            doc = loaders.from_text(synthetic_markdown(size_kb * 1024), f"big-{size_kb}kb")
            t = time.perf_counter()
            result = ingestor.ingest(doc)
            dt = time.perf_counter() - t
            print(f"{size_kb:>5} KB: {result.chunks:>5} chunks in {dt:6.1f} s "
                  f"({result.chunks / dt:.1f} chunks/s)")

        # Search latency while a big ingest is embedding in the background.
        searcher = Searcher(pool, embedder)
        idle = []
        for q in QUESTIONS:
            t = time.perf_counter()
            searcher.search(q)
            idle.append((time.perf_counter() - t) * 1000)
        doc = loaders.from_text(synthetic_markdown(400 * 1024), "during-ingest")
        worker = threading.Thread(target=ingestor.ingest, args=(doc,))
        worker.start()
        time.sleep(2)
        busy = []
        while worker.is_alive() and len(busy) < 60:
            q = QUESTIONS[len(busy) % len(QUESTIONS)]
            t = time.perf_counter()
            searcher.search(q)
            busy.append((time.perf_counter() - t) * 1000)
        worker.join()
        print("\n## Search latency while an ingest is running (same process)")
        print(f"idle          : {describe(idle)}")
        print(f"during ingest : {describe(busy)}")


# ---------------------------------------------------------------- scale

VECTOR_ONLY_SQL = """
SELECT c.id FROM document_chunks c ORDER BY c.embedding <=> %(qvec)s LIMIT %(n)s
"""
KEYWORD_ONLY_SQL = """
WITH terms AS (SELECT phraseto_tsquery('simple', t) AS q FROM unnest(%(terms)s::text[]) t),
w AS (SELECT t.q, ln(1 + (n.total - df.cnt + 0.5) / (df.cnt + 0.5)) AS idf
      FROM terms t CROSS JOIN (SELECT count(*)::float AS total FROM document_chunks) n
      CROSS JOIN LATERAL (SELECT count(*) AS cnt FROM document_chunks c WHERE c.content_tsv @@ t.q) df)
SELECT c.id, sum(w.idf) s FROM document_chunks c JOIN w ON c.content_tsv @@ w.q
GROUP BY c.id ORDER BY s DESC LIMIT %(n)s
"""


def copy_corpus(src_url: str, dst_url: str, factor: int, rng: np.random.Generator) -> float:
    """Load `factor` copies of the real chunks; copies get slightly perturbed vectors
    (so the HNSW graph isn't full of exact duplicates). Indexes dropped during load."""
    with psycopg.connect(src_url) as src:
        db.register_vector(src)
        sources = src.execute("SELECT id, source_uri, title, content_hash, chunk_count, total_tokens FROM sources").fetchall()
        chunks = src.execute("SELECT source_id, chunk_index, heading_path, content, embedding_input, "
                             "token_count, embedding FROM document_chunks").fetchall()
    started = time.perf_counter()
    with psycopg.connect(dst_url) as dst:
        db.register_vector(dst)
        dst.execute("DROP INDEX IF EXISTS idx_document_chunks_embedding")
        dst.execute("DROP INDEX IF EXISTS idx_document_chunks_tsv")
        for copy in range(factor):
            id_map = {}
            with dst.cursor() as cur:
                for sid, uri, title, h, cc, tt in sources:
                    cur.execute("INSERT INTO sources (source_uri, source_type, title, content_hash, chunk_count, "
                                "total_tokens) VALUES (%s, 'file', %s, %s, %s, %s) RETURNING id",
                                (f"{uri}#copy{copy}", title, h, cc, tt))
                    id_map[sid] = cur.fetchone()[0]
                with cur.copy("COPY document_chunks (source_id, chunk_index, heading_path, content, "
                              "embedding_input, token_count, embedding) FROM STDIN") as cp:
                    for sid, idx, path, content, emb_in, tok, vec in chunks:
                        if copy:
                            a = vec.to_numpy().astype(np.float32)
                            v = a + rng.normal(0, 0.02, a.shape[0]).astype(np.float32)
                            vec = v / np.linalg.norm(v)
                        cp.write_row((id_map[sid], idx, path, content, emb_in, tok, vec))
            dst.commit()
    return time.perf_counter() - started


def scale(factors=(1, 10, 50)) -> None:
    embedder = new_embedder()
    rng = np.random.default_rng(0)
    queries = [(q, embedder.embed_query(q), keyword_terms(q)) for q in QUESTIONS]
    print("## Scale test: real corpus replicated (separate DB)")
    print("| chunks | load | HNSW build | GIN build | DB size | vector top-30 p50/p95 | "
          "keyword top-30 p50/p95 | full hybrid p50/p95 |")
    print("|---|---|---|---|---|---|---|---|")
    for factor in factors:
        url = reset_bench_db()
        load_s = copy_corpus(SETTINGS.database_url, url, factor, rng)
        with psycopg.connect(url, autocommit=True) as conn:
            db.register_vector(conn)
            t = time.perf_counter()
            conn.execute("CREATE INDEX idx_document_chunks_embedding ON document_chunks "
                         "USING hnsw (embedding vector_cosine_ops)")
            hnsw_s = time.perf_counter() - t
            t = time.perf_counter()
            conn.execute("CREATE INDEX idx_document_chunks_tsv ON document_chunks USING gin (content_tsv)")
            gin_s = time.perf_counter() - t
            conn.execute("ANALYZE")
            n_chunks = conn.execute("SELECT count(*) FROM document_chunks").fetchone()[0]
            size = conn.execute("SELECT pg_size_pretty(pg_database_size(current_database()))").fetchone()[0]
            plan = "\n".join(r[0] for r in conn.execute(
                "EXPLAIN " + VECTOR_ONLY_SQL, {"qvec": queries[0][1], "n": CANDIDATES_PER_RETRIEVER}))
            vec_ms, kw_ms = [], []
            for _ in range(2):
                for _, qvec, terms in queries:
                    t = time.perf_counter()
                    conn.execute(VECTOR_ONLY_SQL, {"qvec": qvec, "n": CANDIDATES_PER_RETRIEVER}).fetchall()
                    vec_ms.append((time.perf_counter() - t) * 1000)
                    t = time.perf_counter()
                    conn.execute(KEYWORD_ONLY_SQL, {"terms": terms, "n": CANDIDATES_PER_RETRIEVER}).fetchall()
                    kw_ms.append((time.perf_counter() - t) * 1000)
        full_ms = []
        with db.create_pool(url) as pool:
            searcher = Searcher(pool, embedder)
            for _ in range(2):
                for q, qvec, _ in queries:
                    t = time.perf_counter()
                    searcher.search_with_vector(qvec, q)
                    full_ms.append((time.perf_counter() - t) * 1000)
        uses_index = "idx_document_chunks_embedding" in plan
        print(f"| {n_chunks:,} | {load_s:.0f} s | {hnsw_s:.1f} s | {gin_s:.1f} s | {size} | "
              f"{pct(vec_ms, 50):.1f}/{pct(vec_ms, 95):.1f} ms{'' if uses_index else ' (no index!)'} | "
              f"{pct(kw_ms, 50):.1f}/{pct(kw_ms, 95):.1f} ms | {pct(full_ms, 50):.1f}/{pct(full_ms, 95):.1f} ms |",
              flush=True)


if __name__ == "__main__":
    commands = {"latency": latency, "concurrency": concurrency, "ingest": ingest, "scale": scale}
    if len(sys.argv) != 2 or sys.argv[1] not in commands:
        sys.exit(f"usage: benchmark.py {{{'|'.join(commands)}}}")
    commands[sys.argv[1]]()
