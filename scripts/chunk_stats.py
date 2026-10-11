"""Chunk a whole docs repository (no DB, no embeddings) and print statistics.

Usage (inside the image):
    python scripts/chunk_stats.py /corpus [--show path/inside/repo.rst]
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
from pathlib import Path

from rag_mcp import rst
from rag_mcp.chunking import chunk_tree
from rag_mcp.document import format_for_path, parse_document
from rag_mcp.tokens import TokenCounter

EXTENSIONS = {".rst", ".md"}
SKIP_DIRS = {".git", ".github", "node_modules", "includes", "_build"}


def doc_files(root: Path) -> list[Path]:
    return sorted(
        p for p in root.rglob("*")
        if p.suffix in EXTENSIONS and not SKIP_DIRS & set(p.relative_to(root).parts)
    )


def load(path: Path, root: Path) -> str:
    text = path.read_text(encoding="utf-8", errors="replace")
    return rst.resolve_includes(text, path, root) if path.suffix == ".rst" else text


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("root", type=Path)
    ap.add_argument("--show", help="print the chunks of one file")
    args = ap.parse_args()

    counter = TokenCounter.from_pretrained("BAAI/bge-small-en-v1.5")
    if args.show:
        path = args.root / args.show
        for i, c in enumerate(chunk_tree(parse_document(load(path, args.root), format_for_path(path.name)), counter)):
            print(f"--- chunk {i} | {c.token_count} tokens | {' > '.join(c.heading_path)}\n{c.content}\n")
        return 0

    files = doc_files(args.root)
    sizes: list[int] = []
    per_file: list[tuple[int, str]] = []
    chars = 0
    start = time.perf_counter()
    for path in files:
        text = load(path, args.root)
        chars += len(text)
        chunks = chunk_tree(parse_document(text, format_for_path(path.name)), counter)
        sizes.extend(c.token_count for c in chunks)
        per_file.append((len(chunks), str(path.relative_to(args.root))))
    elapsed = time.perf_counter() - start

    q = statistics.quantiles(sizes, n=100)
    print(f"files            {len(files)}")
    print(f"characters       {chars:,} (after include resolution)")
    print(f"chunks           {len(sizes):,}")
    print(f"total tokens     {sum(sizes):,}")
    print(f"tokens/chunk     p5={q[4]:.0f} p50={q[49]:.0f} p95={q[94]:.0f} max={max(sizes)}")
    print(f"chunks <50 tok   {sum(s < 50 for s in sizes)}")
    print(f"empty files      {sum(n == 0 for n, _ in per_file)}")
    print(f"chunking time    {elapsed:.1f}s")
    print("most chunks:", *(f"  {n:4d}  {p}" for n, p in sorted(per_file, reverse=True)[:5]), sep="\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
