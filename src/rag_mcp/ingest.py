"""Ingest pipeline: Document -> chunks -> embeddings -> PostgreSQL.

    hash ─ unchanged? ─ yes ─> skip (no parsing, no embedding)
              │ no
    parse -> chunk -> embed (outside any DB transaction: slow, CPU-bound)
              │
    one transaction: lock source_uri -> upsert source -> delete old chunks -> insert new
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from rag_mcp.chunking import CHUNKER_VERSION, ChunkerConfig, chunk_tree
from rag_mcp.document import document_title, parse_document
from rag_mcp.embedder import Embedder
from rag_mcp.loaders import Collection, Document

log = logging.getLogger(__name__)

Status = Literal["created", "updated", "unchanged"]


@dataclass(frozen=True)
class IngestResult:
    source_uri: str
    status: Status
    chunks: int
    tokens: int


@dataclass
class CollectionReport:
    """Progress and outcome of ingesting a directory/repo. Updated live by the job."""

    description: str
    total: int = 0
    processed: int = 0
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    failed: int = 0
    deleted: int = 0
    sync_skipped: bool = False  # deletion of vanished sources skipped because of failures
    chunks_written: int = 0
    errors: list[str] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)

    def record(self, result: IngestResult) -> None:
        self.processed += 1
        setattr(self, result.status, getattr(self, result.status) + 1)
        if result.status != "unchanged":
            self.chunks_written += result.chunks

    def record_failure(self, what: str, exc: Exception) -> None:
        self.processed += 1
        self.failed += 1
        if len(self.errors) < 20:
            self.errors.append(f"{what}: {exc}")


def content_hash(doc: Document, model_name: str) -> str:
    """Changes when the text, the chunking logic or the embedding model changes."""
    h = hashlib.sha256()
    for part in (CHUNKER_VERSION, model_name, doc.fmt, doc.text):
        h.update(part.encode("utf-8"))
        h.update(b"\0")
    return h.hexdigest()


class Ingestor:
    def __init__(self, pool: ConnectionPool, embedder: Embedder, config: ChunkerConfig = ChunkerConfig()):
        self.pool = pool
        self.embedder = embedder
        self.config = config

    def ingest(self, doc: Document) -> IngestResult:
        digest = content_hash(doc, self.embedder.model_name)
        with self.pool.connection() as conn:
            row = conn.execute(
                "SELECT content_hash, chunk_count, total_tokens FROM sources WHERE source_uri = %s",
                (doc.source_uri,),
            ).fetchone()
        if row and row[0] == digest:
            return IngestResult(doc.source_uri, "unchanged", row[1], row[2])

        tree = parse_document(doc.text, doc.fmt)
        title = doc.title or document_title(tree)
        chunks = chunk_tree(tree, self.embedder.token_counter(), self.config)
        vectors = self.embedder.embed_documents([c.embedding_input for c in chunks]) if chunks else []
        total_tokens = sum(c.token_count for c in chunks)

        with self.pool.connection() as conn, conn.transaction():
            # Serializes concurrent ingests of the same source; the last one wins cleanly.
            conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (doc.source_uri,))
            source_id, inserted = conn.execute(
                """
                INSERT INTO sources (source_uri, source_type, title, content_hash,
                                     chunk_count, total_tokens, metadata)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (source_uri) DO UPDATE SET
                    source_type = EXCLUDED.source_type, title = EXCLUDED.title,
                    content_hash = EXCLUDED.content_hash, chunk_count = EXCLUDED.chunk_count,
                    total_tokens = EXCLUDED.total_tokens, metadata = EXCLUDED.metadata,
                    updated_at = now()
                RETURNING id, (xmax = 0) AS inserted
                """,
                (doc.source_uri, doc.source_type, title, digest, len(chunks), total_tokens, Jsonb(doc.metadata)),
            ).fetchone()
            conn.execute("DELETE FROM document_chunks WHERE source_id = %s", (source_id,))
            with conn.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO document_chunks (source_id, chunk_index, heading_path, content,
                                                 embedding_input, token_count, embedding)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    [
                        (source_id, i, list(c.heading_path), c.content, c.embedding_input, c.token_count, vec)
                        for i, (c, vec) in enumerate(zip(chunks, vectors))
                    ],
                )
        status: Status = "created" if inserted else "updated"
        log.info("%s %s (%d chunks)", status, doc.source_uri, len(chunks))
        return IngestResult(doc.source_uri, status, len(chunks), total_tokens)

    def ingest_collection(
        self,
        collection: Collection,
        report: CollectionReport | None = None,
        on_progress: Callable[[CollectionReport], None] | None = None,
    ) -> CollectionReport:
        """Ingest every document; one failure never aborts the rest.

        Sync: after a complete, failure-free walk, sources under the collection's
        prefix that no longer exist upstream are deleted. After any failure we skip
        that step - a file that failed to load would otherwise look deleted.
        """
        report = report or CollectionReport(collection.description)
        report.total = collection.total
        seen: list[str] = []
        try:
            for path, doc in collection.documents():
                if isinstance(doc, Exception):
                    report.record_failure(path.name, doc)
                else:
                    seen.append(doc.source_uri)  # seen even if ingest fails: it still exists
                    try:
                        report.record(self.ingest(doc))
                    except Exception as exc:
                        log.exception("failed to ingest %s", doc.source_uri)
                        report.record_failure(doc.source_uri, exc)
                if on_progress:
                    on_progress(report)
            # Only reached if the walk completed: never delete after a partial run.
            if report.failed:
                report.sync_skipped = True
            else:
                report.deleted = self._delete_missing(collection.prefix, seen)
        finally:
            collection.cleanup()
        return report

    def _delete_missing(self, prefix: str, seen: list[str]) -> int:
        with self.pool.connection() as conn:
            deleted = conn.execute(
                "DELETE FROM sources WHERE starts_with(source_uri, %s) AND NOT (source_uri = ANY(%s))",
                (prefix, seen),
            ).rowcount
        if deleted:
            log.info("deleted %d sources no longer present under %s", deleted, prefix)
        return deleted
