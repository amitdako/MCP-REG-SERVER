"""Ingest pipeline against a real PostgreSQL: idempotency, atomic replace, sync.

These protect the properties that make re-ingesting a 800-file repo safe and
cheap: unchanged -> no work, changed -> clean replace, failure -> old data intact,
removed upstream -> removed here (but never after a partial run).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rag_mcp import ingest as ingest_mod
from rag_mcp import loaders
from rag_mcp.ingest import Ingestor

pytestmark = pytest.mark.db

DOC = "# Kafka\n\nIntro.\n\n## Limits\n\nMax 1000 topics per service.\n"


def stored_chunks(pool, uri):
    with pool.connection() as conn:
        return conn.execute(
            """SELECT c.chunk_index, c.content, c.heading_path FROM document_chunks c
               JOIN sources s ON s.id = c.source_id WHERE s.source_uri = %s ORDER BY c.chunk_index""",
            (uri,),
        ).fetchall()


def source_row(pool, uri):
    with pool.connection() as conn:
        return conn.execute(
            "SELECT title, chunk_count, total_tokens, updated_at FROM sources WHERE source_uri = %s", (uri,)
        ).fetchone()


def test_new_document_is_stored_with_chunks(pool, fake_embedder):
    result = Ingestor(pool, fake_embedder).ingest(loaders.from_text(DOC, "kafka"))

    assert result.status == "created" and result.chunks == 1
    title, chunk_count, total_tokens, _ = source_row(pool, "text:kafka")
    assert (title, chunk_count) == ("Kafka", 1) and total_tokens == result.tokens > 0
    [(index, content, path)] = stored_chunks(pool, "text:kafka")
    assert index == 0 and "Max 1000 topics" in content and path == ["Kafka"]


def test_unchanged_document_costs_no_embedding(pool, fake_embedder):
    ingestor = Ingestor(pool, fake_embedder)
    ingestor.ingest(loaders.from_text(DOC, "kafka"))
    calls, before = fake_embedder.calls, source_row(pool, "text:kafka")

    result = ingestor.ingest(loaders.from_text(DOC, "kafka"))

    assert result.status == "unchanged" and result.chunks == 1
    assert fake_embedder.calls == calls  # no model work at all
    assert source_row(pool, "text:kafka") == before  # not even rewritten


def test_changed_document_replaces_all_chunks(pool, fake_embedder):
    ingestor = Ingestor(pool, fake_embedder)
    big = "# Guide\n\n" + "\n\n".join(f"## Step {i}\n\n" + "word " * 300 for i in range(4))
    ingestor.ingest(loaders.from_text(big, "guide"))
    assert len(stored_chunks(pool, "text:guide")) > 1

    result = ingestor.ingest(loaders.from_text("# Guide\n\nNow it is short.\n", "guide"))

    assert result.status == "updated"
    rows = stored_chunks(pool, "text:guide")
    assert [r[0] for r in rows] == [0] and "Now it is short." in rows[0][1]  # no leftovers


def test_model_or_chunker_change_forces_reembedding(pool, fake_embedder, token_counter, monkeypatch):
    from conftest import FakeEmbedder

    Ingestor(pool, fake_embedder).ingest(loaders.from_text(DOC, "kafka"))

    other_model = FakeEmbedder(token_counter, model_name="another-model")
    assert Ingestor(pool, other_model).ingest(loaders.from_text(DOC, "kafka")).status == "updated"

    monkeypatch.setattr(ingest_mod, "CHUNKER_VERSION", "999")
    assert Ingestor(pool, other_model).ingest(loaders.from_text(DOC, "kafka")).status == "updated"


def test_failed_write_keeps_previous_version(pool, fake_embedder):
    ingestor = Ingestor(pool, fake_embedder)
    ingestor.ingest(loaders.from_text(DOC, "kafka"))
    before = stored_chunks(pool, "text:kafka")

    class BrokenVectors(type(fake_embedder)):
        def embed_documents(self, texts):
            return super().embed_documents(texts)[:, :10]  # wrong dimension -> insert fails

    broken = BrokenVectors(fake_embedder.token_counter())
    with pytest.raises(Exception):
        Ingestor(pool, broken).ingest(loaders.from_text(DOC + "\nchanged\n", "kafka"))

    assert stored_chunks(pool, "text:kafka") == before  # delete was rolled back too


def test_document_without_text_is_recorded_with_zero_chunks(pool, fake_embedder):
    # Navigation-only pages: the source is listed (and skipped next time), nothing embedded.
    result = Ingestor(pool, fake_embedder).ingest(loaders.from_text("# Just a title\n", "nav"))
    assert result.chunks == 0 and source_row(pool, "text:nav")[1] == 0
    assert fake_embedder.calls == 0


# ---------------------------------------------------------------- collections

def make_repo(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")


def uris(pool):
    with pool.connection() as conn:
        return {r[0] for r in conn.execute("SELECT source_uri FROM sources")}


def test_directory_reingest_syncs_deletions(pool, fake_embedder, tmp_path):
    make_repo(tmp_path, {"a.md": "# A\n\ntext a\n", "b.md": "# B\n\ntext b\n"})
    ingestor = Ingestor(pool, fake_embedder)
    first = ingestor.ingest_collection(loaders.from_directory(tmp_path))
    assert (first.created, first.failed) == (2, 0)
    ingestor.ingest(loaders.from_text("unrelated", "keep-me"))  # outside the directory

    (tmp_path / "b.md").unlink()
    second = ingestor.ingest_collection(loaders.from_directory(tmp_path))

    assert (second.unchanged, second.deleted) == (1, 1)
    remaining = uris(pool)
    assert any(u.endswith("a.md") for u in remaining) and "text:keep-me" in remaining
    assert not any(u.endswith("b.md") for u in remaining)


def test_no_sync_deletion_after_failures(pool, fake_embedder, tmp_path, monkeypatch):
    make_repo(tmp_path, {"a.md": "# A\n\ntext a\n", "b.md": "# B\n\ntext b\n"})
    ingestor = Ingestor(pool, fake_embedder)
    ingestor.ingest_collection(loaders.from_directory(tmp_path))

    # b.md now fails to load (too big); it must not be treated as deleted upstream.
    (tmp_path / "b.md").write_text("# B\n\n" + "x" * 100)
    monkeypatch.setattr(loaders, "MAX_DOCUMENT_BYTES", 50)
    report = ingestor.ingest_collection(loaders.from_directory(tmp_path))

    assert report.failed == 1 and report.sync_skipped and report.deleted == 0
    assert any(u.endswith("b.md") for u in uris(pool))


def test_one_bad_document_does_not_stop_the_rest(pool, fake_embedder, tmp_path, monkeypatch):
    make_repo(tmp_path, {f"d{i}.md": f"# D{i}\n\ntext {i}\n" for i in range(5)})
    ingestor = Ingestor(pool, fake_embedder)
    real_ingest = ingestor.ingest

    def flaky(doc):
        if doc.source_uri.endswith("d2.md"):
            raise RuntimeError("boom")
        return real_ingest(doc)

    monkeypatch.setattr(ingestor, "ingest", flaky)
    report = ingestor.ingest_collection(loaders.from_directory(tmp_path))
    assert (report.created, report.failed, report.processed) == (4, 1, 5)
    assert "boom" in report.errors[0]
