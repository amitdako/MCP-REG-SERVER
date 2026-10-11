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
