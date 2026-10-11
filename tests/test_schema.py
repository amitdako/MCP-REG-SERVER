"""Schema tests: the database itself enforces the integrity rules ingest relies on."""

from __future__ import annotations

import numpy as np
import psycopg
import pytest

from rag_mcp import db

pytestmark = pytest.mark.db

DIM = 384


def _add_source(conn, uri="docs/a.rst", source_type="file") -> int:
    return conn.execute(
        "INSERT INTO sources (source_uri, source_type, content_hash) VALUES (%s, %s, 'h') RETURNING id",
        (uri, source_type),
    ).fetchone()[0]


def _add_chunk(conn, source_id: int, index: int, text: str = "hello world") -> None:
    conn.execute(
        """INSERT INTO document_chunks
           (source_id, chunk_index, content, embedding_input, token_count, embedding)
           VALUES (%s, %s, %s, %s, 2, %s)""",
        (source_id, index, text, text, np.ones(DIM, dtype=np.float32)),
    )


def test_ensure_schema_is_idempotent(clean_db, database_url):
    # Runs on every server start, so a second (and third) run must be a no-op.
    db.ensure_schema(database_url, DIM)
    db.ensure_schema(database_url, DIM)


def test_dimension_mismatch_fails_loudly(clean_db, database_url):
    # Switching to e.g. a 768-dim model on an existing volume must not fail later, mid-ingest.
    with pytest.raises(db.SchemaMismatchError):
        db.ensure_schema(database_url, 768)


def test_deleting_source_cascades_to_chunks(clean_db):
    sid = _add_source(clean_db)
    _add_chunk(clean_db, sid, 0)
    _add_chunk(clean_db, sid, 1)
    clean_db.execute("DELETE FROM sources WHERE id = %s", (sid,))
    assert clean_db.execute("SELECT count(*) FROM document_chunks").fetchone()[0] == 0


def test_duplicate_chunk_index_rejected(clean_db):
    sid = _add_source(clean_db)
    _add_chunk(clean_db, sid, 0)
    with pytest.raises(psycopg.errors.UniqueViolation):
        _add_chunk(clean_db, sid, 0)


def test_duplicate_source_uri_rejected(clean_db):
    _add_source(clean_db, "docs/a.rst")
    with pytest.raises(psycopg.errors.UniqueViolation):
        _add_source(clean_db, "docs/a.rst")


def test_unknown_source_type_rejected(clean_db):
    with pytest.raises(psycopg.errors.CheckViolation):
        _add_source(clean_db, "x", source_type="pdf")


def test_full_text_column_matches_identifiers_exactly(clean_db):
    # 'simple' config: no stemming, so a parameter name is findable by keyword search.
    sid = _add_source(clean_db)
    _add_chunk(clean_db, sid, 0, "Set max_connections to limit concurrent clients")
    hits = clean_db.execute(
        "SELECT count(*) FROM document_chunks WHERE content_tsv @@ plainto_tsquery('simple', %s)",
        ("max_connections",),
    ).fetchone()[0]
    assert hits == 1
