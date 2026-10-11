"""Database connection and schema management."""

from __future__ import annotations

import logging
from importlib import resources

import psycopg
from pgvector.psycopg import register_vector
from psycopg_pool import ConnectionPool

log = logging.getLogger(__name__)

# Arbitrary constant: serializes schema creation when several server
# processes start at once (concurrent CREATE ... IF NOT EXISTS can still race).
_SCHEMA_LOCK_ID = 7_316_001


class SchemaMismatchError(RuntimeError):
    pass


def connect(database_url: str) -> psycopg.Connection:
    conn = psycopg.connect(database_url)
    register_vector(conn)
    return conn


def create_pool(database_url: str, max_size: int = 4) -> ConnectionPool:
    """Pool shared by tool calls and the background ingest worker.
    Call ensure_schema first: register_vector needs the extension to exist."""
    return ConnectionPool(
        database_url, min_size=1, max_size=max_size, configure=register_vector, open=True,
    )


def schema_sql(embedding_dim: int) -> str:
    template = resources.files("rag_mcp").joinpath("schema.sql").read_text(encoding="utf-8")
    return template.replace("__EMBEDDING_DIM__", str(int(embedding_dim)))


def ensure_schema(database_url: str, embedding_dim: int) -> None:
    """Create the extension, tables and indexes if missing. Safe to call on every start."""
    # Plain connection: register_vector needs the extension to exist first.
    with psycopg.connect(database_url) as conn:
        with conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (_SCHEMA_LOCK_ID,))
            conn.execute(schema_sql(embedding_dim))
            existing = _embedding_column_dim(conn)
    if existing != embedding_dim:
        raise SchemaMismatchError(
            f"document_chunks.embedding is vector({existing}) but the configured model "
            f"produces {embedding_dim} dimensions. Use the original model, or drop the "
            f"data volume (docker compose down -v) and re-ingest."
        )
    log.info("schema ready (embedding dim %d)", embedding_dim)


def _embedding_column_dim(conn: psycopg.Connection) -> int:
    # For pgvector, atttypmod holds the declared dimension.
    row = conn.execute(
        """
        SELECT atttypmod FROM pg_attribute
        WHERE attrelid = 'document_chunks'::regclass AND attname = 'embedding'
        """
    ).fetchone()
    return int(row[0])
