"""The real local model: right shape, normalized, query prefix, sane ranking."""

from __future__ import annotations

import numpy as np
import pytest

pytest.importorskip("sentence_transformers")

from rag_mcp.embedder import BGE_QUERY_PREFIX, SentenceTransformerEmbedder  # noqa: E402


@pytest.fixture(scope="module")
def embedder():
    e = SentenceTransformerEmbedder("BAAI/bge-small-en-v1.5", 384)
    e.start_loading()  # background load, as in the server; calls below wait for it
    return e


def test_vectors_have_configured_dim_and_unit_length(embedder):
    vecs = embedder.embed_documents(["first text", "second text"])
    assert vecs.shape == (2, 384)
    assert np.allclose(np.linalg.norm(vecs, axis=1), 1.0, atol=1e-4)


def test_query_uses_bge_instruction_prefix(embedder):
    q = embedder.embed_query("max connections")
    plain = embedder.embed_documents(["max connections"])[0]
    prefixed = embedder.embed_documents([BGE_QUERY_PREFIX + "max connections"])[0]
    assert np.allclose(q, prefixed, atol=1e-5) and not np.allclose(q, plain, atol=1e-3)


def test_relevant_passage_ranks_first(embedder):
    docs = [
        "The maximum number of PostgreSQL connections depends on the service plan's memory.",
        "Kafka topics can be created with the Aiven CLI.",
        "Grafana dashboards visualize metrics.",
    ]
    scores = embedder.embed_documents(docs) @ embedder.embed_query("How many connections can my Postgres have?")
    assert int(np.argmax(scores)) == 0


def test_wrong_dimension_config_fails_clearly():
    e = SentenceTransformerEmbedder("BAAI/bge-small-en-v1.5", 768)
    with pytest.raises(RuntimeError, match="384 dims"):
        e.embed_query("x")


def test_search_is_not_starved_by_a_running_ingest(embedder):
    # Regression: with a plain lock, the ingest thread re-acquired it batch after batch
    # and a search waited for the whole document (measured up to ~40 s).
    import threading
    import time

    texts = [f"document number {i} about kafka topics and connection limits " * 8 for i in range(480)]
    ingest = threading.Thread(target=embedder.embed_documents, args=(texts,))
    started = time.perf_counter()
    ingest.start()
    time.sleep(0.5)
    waits = []
    while ingest.is_alive() and len(waits) < 5:
        t = time.perf_counter()
        embedder.embed_query("how many topics can kafka have")
        waits.append(time.perf_counter() - t)
    ingest.join()
    total = time.perf_counter() - started
    assert waits, "ingest finished too fast to measure"
    assert max(waits) < total / 3  # a search waits for one batch, not the whole ingest
