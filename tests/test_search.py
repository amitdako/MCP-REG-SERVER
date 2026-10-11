"""Hybrid search with the real model on a small, controlled corpus."""

from __future__ import annotations

import pytest

from rag_mcp import loaders
from rag_mcp.ingest import Ingestor
from rag_mcp.search import Fusion, Searcher, format_hits, is_identifier, keyword_terms

pytestmark = pytest.mark.db
pytest.importorskip("sentence_transformers")

# Mirrors the real devportal page (idle-connections.rst), including its intro sentence.
KEEPALIVE = """# Keep-alive connections parameters

PostgreSQL keep-alive connection parameters are useful to manage idle connections. The following
are the default Aiven for PostgreSQL parameters on the server side.

| Parameter | Value | Description |
| `tcp_keepalives_idle` | 180 | Seconds of inactivity before a TCP keepalive is sent. |
| `tcp_keepalives_count` | 6 | Lost keepalives before the connection is dead. |
"""


@pytest.fixture(scope="module")
def embedder():
    from rag_mcp.embedder import SentenceTransformerEmbedder

    e = SentenceTransformerEmbedder("BAAI/bge-small-en-v1.5", 384)
    e.start_loading()
    return e


@pytest.fixture
def corpus(pool, embedder):
    ingestor = Ingestor(pool, embedder)
    # Noise that repeats the common words of the test question many times.
    for i in range(15):
        ingestor.ingest(loaders.from_text(
            f"# Aiven for PostgreSQL guide {i}\n\nAiven for PostgreSQL default settings. Aiven PostgreSQL "
            f"is the default PostgreSQL on Aiven. Topic {i}: backups, default plans, Aiven PostgreSQL.",
            f"noise-{i}"))
    ingestor.ingest(loaders.from_text(KEEPALIVE, "keepalive"))
    ingestor.ingest(loaders.from_text(
        "# Kafka topics\n\nCreate topics automatically by enabling `auto_create_topics_enable`.", "kafka"))
    return Searcher(pool, embedder)


def test_keyword_terms_drop_stopwords_keep_identifiers():
    assert keyword_terms("What is the default tcp_keepalives_idle for Aiven?") == \
        ["default", "tcp_keepalives_idle", "aiven"]
    assert keyword_terms("How do I set kafka.consumer.timeout to 30?") == ["set", "kafka.consumer.timeout", "30"]


def test_identifier_detection():
    assert all(map(is_identifier, ["max_connections", "kafka.consumer.timeout", "pg16"]))
    assert not any(map(is_identifier, ["connections", "kafka", "30"]))


def test_single_sided_fusion_modes(corpus):
    # Used by the eval to compare vector-only / keyword-only against hybrid.
    vector_only = corpus.search("tcp keepalive idle", top_k=10, fusion=Fusion(keyword_weight=0))
    keyword_only = corpus.search("tcp keepalive idle", top_k=10, fusion=Fusion(vector_weight=0))
    assert vector_only and all(h.vector_rank for h in vector_only)
    assert keyword_only and all(h.keyword_rank for h in keyword_only)


QUESTION = "What is the default tcp_keepalives_idle for Aiven for PostgreSQL?"


def test_keyword_side_ranks_rare_identifier_first(corpus):
    # Regression: with ts_rank (no IDF) 15 pages repeating "Aiven PostgreSQL default" won
    # the keyword side. With IDF weighting, the one chunk containing the identifier is #1.
    hit = next(h for h in corpus.search(QUESTION, top_k=17) if h.source_uri == "text:keepalive")
    assert hit.keyword_rank == 1


def test_parameter_question_finds_the_parameter_page(corpus):
    assert corpus.search(QUESTION)[0].source_uri == "text:keepalive"


def test_semantic_question_without_shared_keywords(corpus):
    hits = corpus.search("How can topics be made on the fly when a producer writes to them?")
    assert hits[0].source_uri == "text:kafka"


def test_source_filter_restricts_results(corpus):
    hits = corpus.search("default settings", top_k=10, source_filter="noise-1")
    assert hits and all("noise-1" in h.source_uri for h in hits)  # noise-1, noise-10..14


def test_results_carry_citation_and_breadcrumb(corpus):
    hits = corpus.search("tcp keepalive idle", top_k=3)
    text = format_hits("tcp keepalive idle", hits)
    assert "[1] Keep-alive connections parameters" in text
    assert "source: text:keepalive" in text and "`tcp_keepalives_idle` | 180" in text


def test_empty_index_gives_helpful_message(pool, embedder):
    hits = Searcher(pool, embedder).search("anything")
    assert hits == [] and "list_sources" in format_hits("anything", hits)
