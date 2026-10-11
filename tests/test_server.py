"""End to end over real stdio, exactly how Claude Code talks to the server.

Spawns `python -m rag_mcp serve` as a subprocess and drives it with the MCP
client. This catches what unit tests can't: anything printed to stdout breaks
the protocol, tool schemas, error reporting, and startup wiring.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

import pytest

pytestmark = pytest.mark.db
pytest.importorskip("sentence_transformers")

from mcp import Client  # noqa: E402
from mcp.client.stdio import StdioServerParameters  # noqa: E402

DOC = "# Kafka limits\n\n## Topics\n\nA service supports at most 1000 topics. Set `max_topics` to change it.\n"


def server_params(database_url: str) -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable, args=["-m", "rag_mcp", "serve"],
        env={**os.environ, "DATABASE_URL": database_url, "LOG_LEVEL": "WARNING"},
    )


def text_of(result) -> str:
    return "\n".join(c.text for c in result.content if getattr(c, "text", None))


def test_full_session_over_stdio(clean_db, database_url):
    async def session():
        async with Client(server_params(database_url), read_timeout_seconds=120) as client:
            started = time.perf_counter()
            tools = {t.name for t in (await client.list_tools()).tools}
            assert tools == {"ingest", "ingest_status", "search", "list_sources", "delete_source"}

            ingested = await client.call_tool("ingest", {"text": DOC, "name": "kafka-limits"})
            assert not ingested.is_error and text_of(ingested).startswith("created: text:kafka-limits")

            again = await client.call_tool("ingest", {"text": DOC, "name": "kafka-limits"})
            assert text_of(again).startswith("unchanged")

            found = await client.call_tool("search", {"query": "How many topics can a Kafka service have?"})
            body = text_of(found)
            assert "[1] Kafka limits" in body and "source: text:kafka-limits" in body and "1000 topics" in body

            listed = text_of(await client.call_tool("list_sources", {"filter": "kafka"}))
            assert "Total: 1 sources" in listed and "text:kafka-limits" in listed

            bad = await client.call_tool("ingest", {"text": "x", "url": "https://example.com"})
            assert bad.is_error and "exactly one" in text_of(bad)

            deleted = await client.call_tool("delete_source", {"source_uri": "text:kafka-limits"})
            assert "deleted text:kafka-limits (1 chunks)" in text_of(deleted)
            missing = await client.call_tool("delete_source", {"source_uri": "text:kafka-limits"})
            assert missing.is_error
            return time.perf_counter() - started

    asyncio.run(session())


def test_directory_ingest_runs_as_background_job(clean_db, database_url, tmp_path):
    for i in range(3):
        (tmp_path / f"doc{i}.md").write_text(f"# Doc {i}\n\nContent number {i}.\n")

    async def session():
        async with Client(server_params(database_url), read_timeout_seconds=120) as client:
            started = text_of(await client.call_tool("ingest", {"path": str(tmp_path)}))
            assert "Started background ingest job" in started
            job_id = started.split("job ")[1].split()[0]
            for _ in range(240):
                status = text_of(await client.call_tool("ingest_status", {"job_id": job_id}))
                if '"state": "done"' in status:
                    break
                await asyncio.sleep(0.5)
            assert '"created": 3' in status and '"failed": 0' in status

    asyncio.run(session())
