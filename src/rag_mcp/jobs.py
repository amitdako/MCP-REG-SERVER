"""Background ingest jobs for big inputs (directories, GitHub repos).

A whole-repo ingest takes minutes (embedding is ~4 chunks/s on CPU), longer
than an MCP tool call should block. `submit` returns at once; the work runs on a
single worker thread (ingest is CPU-bound, so parallel jobs would only compete),
and documents become searchable one by one as they are committed.

Jobs live in memory: if the server process exits, a job is lost - re-running
the same ingest resumes cheaply because unchanged documents are skipped by hash.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Literal

from rag_mcp.ingest import CollectionReport, Ingestor
from rag_mcp.loaders import Collection

log = logging.getLogger(__name__)

JobState = Literal["queued", "running", "done", "failed"]


@dataclass
class Job:
    id: str
    description: str
    state: JobState = "queued"
    report: CollectionReport | None = None
    error: str | None = None
    submitted_at: float = field(default_factory=time.time)
    finished_at: float | None = None

    def summary(self) -> dict:
        out: dict = {"job_id": self.id, "input": self.description, "state": self.state}
        if self.error:
            out["error"] = self.error
        r = self.report
        if r:
            elapsed = (self.finished_at or time.time()) - r.started_at
            out.update(
                files_total=r.total, files_processed=r.processed, created=r.created,
                updated=r.updated, unchanged=r.unchanged, failed=r.failed,
                deleted_missing=r.deleted, chunks_written=r.chunks_written,
                elapsed_s=round(elapsed),
            )
            if self.state == "running" and r.processed:
                out["eta_s"] = round(elapsed / r.processed * (r.total - r.processed))
            if r.sync_skipped:
                out["note"] = "some files failed, so sources missing upstream were not deleted"
            if r.errors:
                out["errors"] = r.errors
        return out


class JobManager:
    def __init__(self, ingestor: Ingestor) -> None:
        self._ingestor = ingestor
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ingest-job")
        self._jobs: dict[str, Job] = {}

    def submit(self, description: str, open_collection: Callable[[], Collection]) -> Job:
        """open_collection runs on the worker (cloning a repo can take a while)."""
        job = Job(id=uuid.uuid4().hex[:8], description=description)
        self._jobs[job.id] = job
        self._executor.submit(self._run, job, open_collection)
        return job

    def _run(self, job: Job, open_collection: Callable[[], Collection]) -> None:
        job.state = "running"
        try:
            collection = open_collection()
            job.report = CollectionReport(collection.description, total=collection.total)
            self._ingestor.ingest_collection(collection, job.report)
            job.state = "done"
        except Exception as exc:
            log.exception("ingest job %s failed", job.id)
            job.state, job.error = "failed", str(exc)
        finally:
            job.finished_at = time.time()

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def all(self) -> list[Job]:
        return sorted(self._jobs.values(), key=lambda j: j.submitted_at, reverse=True)

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)
