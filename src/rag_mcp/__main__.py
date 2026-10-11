"""Command-line entry point: `python -m rag_mcp <command>`.

Logging always goes to stderr: in `serve` mode stdout carries the MCP protocol,
and a single stray print would corrupt it.
"""

from __future__ import annotations

import argparse
import logging
import sys

from rag_mcp import db
from rag_mcp.config import Settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rag_mcp")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init-db", help="create the database schema (also done on server start)")
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    logging.basicConfig(
        stream=sys.stderr,
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.command == "init-db":
        db.ensure_schema(settings.database_url, settings.embedding_dim)
    return 0


if __name__ == "__main__":
    sys.exit(main())
