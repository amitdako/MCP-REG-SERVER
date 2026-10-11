from __future__ import annotations

import os

import psycopg
import pytest
from psycopg import sql

from rag_mcp import db

DIM = 384


def _test_database_url() -> str:
    return os.environ.get("DATABASE_URL", "postgresql://rag:rag@localhost:5433/rag_test")


def _create_database_if_missing(url: str) -> None:
    info = psycopg.conninfo.conninfo_to_dict(url)
    name = info["dbname"]
    admin_url = psycopg.conninfo.make_conninfo(url, dbname="postgres")
    with psycopg.connect(admin_url, autocommit=True) as conn:
        exists = conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone()
        if not exists:
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))


@pytest.fixture(scope="session")
def database_url() -> str:
    url = _test_database_url()
    if not url.rstrip("/").endswith("_test"):
        pytest.exit(f"refusing to run DB tests against a non-test database: {url}")
    try:
        _create_database_if_missing(url)
    except psycopg.OperationalError as exc:
        pytest.skip(f"PostgreSQL not available: {exc}")
    return url


@pytest.fixture
def clean_db(database_url: str):
    """A freshly created schema for each test."""
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute("DROP TABLE IF EXISTS document_chunks, sources CASCADE")
    db.ensure_schema(database_url, DIM)
    with db.connect(database_url) as conn:
        yield conn


# ---------------------------------------------------------------- pipeline fixtures

class FakeEmbedder:
    """Deterministic vectors + call counting: lets tests assert *whether* embedding
    happened (e.g. unchanged documents must cost zero model calls)."""

    def __init__(self, counter, model_name: str = "fake-model", dim: int = DIM) -> None:
        self.model_name = model_name
        self.dim = dim
        self.calls = 0
        self.texts_embedded = 0
        self._counter = counter

    def embed_documents(self, texts):
        import hashlib

        import numpy as np

        self.calls += 1
        self.texts_embedded += len(texts)
        out = []
        for t in texts:
            seed = int.from_bytes(hashlib.sha256(t.encode()).digest()[:8], "little")
            v = np.random.default_rng(seed).normal(size=self.dim).astype(np.float32)
            out.append(v / np.linalg.norm(v))
        return np.stack(out)

    def embed_query(self, text):
        return self.embed_documents([text])[0]

    def token_counter(self):
        return self._counter


@pytest.fixture(scope="session")
def token_counter():
    pytest.importorskip("transformers")
    from rag_mcp.tokens import TokenCounter

    return TokenCounter.from_pretrained("BAAI/bge-small-en-v1.5")


@pytest.fixture
def pool(clean_db, database_url):
    with db.create_pool(database_url) as p:
        yield p


@pytest.fixture
def fake_embedder(token_counter):
    return FakeEmbedder(token_counter)
