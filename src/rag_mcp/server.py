"""MCP server (stdio): the tools Claude uses to ingest and retrieve.

Startup order matters: the schema is checked (fast, fails loudly if the DB is
unreachable), the embedding model starts loading in a background thread, and
the server answers the MCP handshake immediately. Tools that need the model
wait for it; nothing blocks the handshake on the ~10 s torch import.

Tool functions are synchronous: the SDK runs them in worker threads, so a slow
call never blocks the protocol loop.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import mcp.types as types
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from rag_mcp import db, loaders
from rag_mcp.config import Settings
from rag_mcp.embedder import SentenceTransformerEmbedder
from rag_mcp.ingest import Ingestor
from rag_mcp.jobs import JobManager
from rag_mcp.search import Searcher, format_hits

log = logging.getLogger(__name__)

DEFAULT_TOP_K = 5
MAX_TOP_K = 20
MAX_LIST_LIMIT = 200

INSTRUCTIONS = """\
Retrieval over ingested documentation (by default: the Aiven docs repository).
- Before answering a question about the ingested content, call `search`. Answer only from
  the returned chunks and cite the `source` URL of the chunks you used.
- For multi-part questions, search once per part. If results look off-topic, rephrase with
  the exact product/parameter names, or narrow with `source_filter` (e.g. "products/kafka").
- If nothing relevant comes back, say so instead of guessing.
- Ingesting a directory or GitHub repo runs in the background; follow it with `ingest_status`.
"""

READ_ONLY = types.ToolAnnotations(read_only_hint=True, open_world_hint=False)


class RagService:
    """Wires the components together; shared by all tool calls."""

    def __init__(self, settings: Settings) -> None:
        db.ensure_schema(settings.database_url, settings.embedding_dim)
        self.pool = db.create_pool(settings.database_url)
        self.embedder = SentenceTransformerEmbedder(
            settings.embedding_model, settings.embedding_dim, batch_size=settings.ingest_batch_size
        )
        self.embedder.start_loading()
        self.ingestor = Ingestor(self.pool, self.embedder)
        self.searcher = Searcher(self.pool, self.embedder)
        self.jobs = JobManager(self.ingestor)

    def close(self) -> None:
        self.jobs.shutdown()
        self.pool.close()


def build_server(service: RagService) -> MCPServer:
    server = MCPServer(name="rag-docs", instructions=INSTRUCTIONS)

    @server.tool(
        annotations=types.ToolAnnotations(read_only_hint=False, destructive_hint=False,
                                          idempotent_hint=True, open_world_hint=True),
    )
    def ingest(text: str | None = None, path: str | None = None, url: str | None = None,
               name: str | None = None) -> str:
        """Store content for retrieval. Provide exactly one of:
        - text: raw text/Markdown (give `name` to replace it later by re-ingesting with the same name)
        - path: a file or directory inside the server container (host ./data is mounted at /data)
        - url: a web page, a raw .md/.rst/.txt file, a GitHub file, or a GitHub repo/folder
          (e.g. https://github.com/aiven/devportal)

        Directories and GitHub repos/folders are ingested in the background: this returns a
        job id at once; use `ingest_status`. Re-ingesting is cheap: unchanged documents are
        skipped, changed ones replaced, and files removed from a repo/directory are deleted.
        """
        if sum(bool(v) for v in (text, path, url)) != 1:
            raise ToolError("provide exactly one of: text, path, url")
        try:
            if url:
                ref = loaders.parse_github_url(url)
                if ref and ref.kind != "blob":
                    return _start_job(service, f"GitHub {url}", lambda: loaders.from_github(ref))
            if path and Path(path).expanduser().is_dir():
                return _start_job(service, f"directory {path}",
                                  lambda: loaders.from_directory(Path(path).expanduser()))
            target = loaders.resolve(text=text, path=path, url=url, name=name)
            result = service.ingestor.ingest(target)
        except loaders.LoaderError as exc:
            raise ToolError(str(exc)) from exc
        return (f"{result.status}: {result.source_uri} "
                f"({result.chunks} chunks, {result.tokens} tokens)")

    @server.tool(annotations=READ_ONLY)
    def ingest_status(job_id: str | None = None) -> str:
        """Progress of background ingest jobs: files processed/total, created/updated/unchanged/
        failed counts, ETA, and errors. Omit job_id to see all jobs of this server session."""
        if job_id:
            job = service.jobs.get(job_id)
            if not job:
                raise ToolError(f"unknown job id {job_id!r} (jobs are kept only while the server runs)")
            return json.dumps(job.summary(), indent=2)
        jobs = service.jobs.all()
        return json.dumps([j.summary() for j in jobs], indent=2) if jobs else "No ingest jobs in this session."

    @server.tool(annotations=READ_ONLY)
    def search(query: str, top_k: int = DEFAULT_TOP_K, source_filter: str | None = None) -> str:
        """Find the most relevant chunks of ingested content for a question or keywords.
        Combines semantic (vector) and exact keyword matching, so parameter names like
        `max_connections` are matched exactly. Each result has a breadcrumb (page > section),
        its source URL, and the text.

        Args:
            query: the question, ideally with exact product and parameter names
            top_k: number of results (1-20, default 5)
            source_filter: only sources whose URI contains this text, e.g. "products/kafka"
        """
        if not query.strip():
            raise ToolError("query is empty")
        if not 1 <= top_k <= MAX_TOP_K:
            raise ToolError(f"top_k must be between 1 and {MAX_TOP_K}")
        hits = service.searcher.search(query, top_k=top_k, source_filter=source_filter)
        return format_hits(query, hits)

    @server.tool(annotations=READ_ONLY)
    def list_sources(filter: str | None = None, limit: int = 50, offset: int = 0) -> str:
        """What has been ingested: totals, running jobs, then sources (newest first).

        Args:
            filter: only sources whose URI or title contains this text (case-insensitive)
            limit: rows per page (max 200); offset: rows to skip, for paging
        """
        if not 1 <= limit <= MAX_LIST_LIMIT or offset < 0:
            raise ToolError(f"limit must be 1-{MAX_LIST_LIMIT} and offset >= 0")
        return _list_sources(service, filter, limit, offset)

    @server.tool(
        annotations=types.ToolAnnotations(read_only_hint=False, destructive_hint=True,
                                          idempotent_hint=True, open_world_hint=False),
    )
    def delete_source(source_uri: str) -> str:
        """Delete one ingested source and all its chunks. Needs the exact source_uri
        (as shown by list_sources or in search results)."""
        with service.pool.connection() as conn:
            row = conn.execute(
                "DELETE FROM sources WHERE source_uri = %s RETURNING chunk_count", (source_uri,)
            ).fetchone()
        if not row:
            raise ToolError(f"no source {source_uri!r}; use list_sources(filter=...) to find the exact URI")
        return f"deleted {source_uri} ({row[0]} chunks)"

    return server


def _start_job(service: RagService, description: str, open_collection) -> str:
    job = service.jobs.submit(description, open_collection)
    return (f"Started background ingest job {job.id} for {description}. Documents become "
            f"searchable as they are processed. Check progress with ingest_status(job_id=\"{job.id}\").")


def _list_sources(service: RagService, text_filter: str | None, limit: int, offset: int) -> str:
    pattern = f"%{text_filter}%" if text_filter else None
    with service.pool.connection() as conn:
        totals = conn.execute(
            """SELECT count(*), coalesce(sum(chunk_count), 0), coalesce(sum(total_tokens), 0)
               FROM sources"""
        ).fetchone()
        by_type = conn.execute(
            "SELECT source_type, count(*) FROM sources GROUP BY 1 ORDER BY 1"
        ).fetchall()
        where = "WHERE source_uri ILIKE %(p)s OR title ILIKE %(p)s" if pattern else ""
        matching = conn.execute(f"SELECT count(*) FROM sources {where}", {"p": pattern}).fetchone()[0]
        rows = conn.execute(
            f"""SELECT source_uri, title, chunk_count, updated_at FROM sources {where}
                ORDER BY updated_at DESC, source_uri LIMIT %(limit)s OFFSET %(offset)s""",
            {"p": pattern, "limit": limit, "offset": offset},
        ).fetchall()

    lines = [
        f"Total: {totals[0]} sources, {totals[1]} chunks, {totals[2]:,} tokens "
        f"({', '.join(f'{n} {t}' for t, n in by_type) or 'nothing ingested yet'})."
    ]
    running = [j for j in service.jobs.all() if j.state in ("queued", "running")]
    for job in running:
        s = job.summary()
        lines.append(f"Ingest job {job.id} {job.state}: {s.get('files_processed', 0)}/"
                     f"{s.get('files_total', '?')} files ({job.description}).")
    scope = f" matching {text_filter!r}" if text_filter else ""
    lines.append(f"\nShowing {len(rows)} of {matching} sources{scope} (offset {offset}):")
    lines += [f"- {title or '(untitled)'} | {uri} | {chunks} chunks | updated {updated:%Y-%m-%d %H:%M}"
              for uri, title, chunks, updated in rows]
    if offset + len(rows) < matching:
        lines.append(f"... use offset={offset + len(rows)} for more.")
    return "\n".join(lines)


def serve(settings: Settings) -> None:
    service = RagService(settings)
    try:
        build_server(service).run("stdio")
    finally:
        service.close()
