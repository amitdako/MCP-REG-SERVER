"""Command-line entry point: `python -m rag_mcp <command>`.

Logging always goes to stderr: in `serve` mode stdout carries the MCP protocol,
and a single stray print would corrupt it.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys

from rag_mcp import db, loaders
from rag_mcp.config import Settings

# Library chatter (download/progress bars) must never reach stdout.
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rag_mcp")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init-db", help="create the database schema (also done on server start)")
    ingest = sub.add_parser("ingest", help="ingest a file, directory or URL synchronously (no time limit)")
    ingest.add_argument("source", help="path inside the container (e.g. /data/x.md), URL, or GitHub repo URL")
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    logging.basicConfig(
        stream=sys.stderr,
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    db.ensure_schema(settings.database_url, settings.embedding_dim)
    if args.command == "ingest":
        return _ingest(settings, args.source)
    return 0


def _ingest(settings: Settings, source: str) -> int:
    from rag_mcp.embedder import SentenceTransformerEmbedder
    from rag_mcp.ingest import Ingestor

    is_url = source.startswith(("http://", "https://"))
    target = loaders.resolve(url=source) if is_url else loaders.resolve(path=source)
    embedder = SentenceTransformerEmbedder(
        settings.embedding_model, settings.embedding_dim, batch_size=settings.ingest_batch_size
    )
    with db.create_pool(settings.database_url) as pool:
        ingestor = Ingestor(pool, embedder)
        if isinstance(target, loaders.Document):
            result = ingestor.ingest(target)
            print(json.dumps(result.__dict__, indent=2))
            return 0

        def progress(r):
            if r.processed % 25 == 0 or r.processed == r.total:
                logging.info("%d/%d files (created %d, updated %d, unchanged %d, failed %d)",
                             r.processed, r.total, r.created, r.updated, r.unchanged, r.failed)

        report = ingestor.ingest_collection(target, on_progress=progress)
        print(json.dumps({k: v for k, v in report.__dict__.items() if k != "started_at"}, indent=2))
        return 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(main())
