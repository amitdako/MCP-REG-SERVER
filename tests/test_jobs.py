"""Background jobs: run off the caller's thread, report progress, surface failures."""

from __future__ import annotations

import time

import pytest

from rag_mcp import loaders
from rag_mcp.ingest import Ingestor
from rag_mcp.jobs import JobManager

pytestmark = pytest.mark.db


def wait_for(job, timeout=60):
    deadline = time.time() + timeout
    while job.state in ("queued", "running") and time.time() < deadline:
        time.sleep(0.05)
    return job


def test_job_ingests_directory_and_reports(pool, fake_embedder, tmp_path):
    for i in range(3):
        (tmp_path / f"d{i}.md").write_text(f"# Doc {i}\n\nbody {i}\n")
    manager = JobManager(Ingestor(pool, fake_embedder))

    job = manager.submit("test dir", lambda: loaders.from_directory(tmp_path))
    summary = wait_for(job).summary()

    assert summary["state"] == "done"
    assert (summary["files_total"], summary["created"], summary["failed"]) == (3, 3, 0)
    assert manager.get(job.id) is job and manager.all() == [job]
    manager.shutdown()


def test_job_failure_is_reported_not_raised(pool, fake_embedder):
    manager = JobManager(Ingestor(pool, fake_embedder))

    def boom():
        raise loaders.LoaderError("git clone failed: repository not found")

    summary = wait_for(manager.submit("bad repo", boom)).summary()
    assert summary["state"] == "failed" and "repository not found" in summary["error"]
    manager.shutdown()
