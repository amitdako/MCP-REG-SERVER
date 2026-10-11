"""Retrieval quality on the eval set, for several fusion configurations.

Usage (needs the devportal repo ingested):
    docker compose run --rm --entrypoint python mcp-server scripts/eval_retrieval.py

A question is answered correctly at rank r if the r-th returned chunk comes from
one of its expected pages. Metrics: hit@1, hit@5 (what Claude sees by default)
and MRR@10.
"""

from __future__ import annotations

import json
import sys
import time
from collections import defaultdict
from pathlib import Path

from rag_mcp import db
from rag_mcp.config import Settings
from rag_mcp.embedder import SentenceTransformerEmbedder
from rag_mcp.search import Fusion, Searcher

QUESTIONS = Path(__file__).resolve().parent.parent / "eval" / "questions.jsonl"
DEPTH = 10

CONFIGS = {
    "vector only": Fusion(keyword_weight=0),
    "keyword only": Fusion(vector_weight=0),
    "hybrid (RRF)": Fusion(),
    "hybrid + id boost x2": Fusion(identifier_boost=2.0),
}


def page(source_uri: str) -> str:
    return source_uri.split("/blob/", 1)[-1].split("/", 1)[-1]  # strip ".../blob/<branch>/"


def first_correct_rank(hits, expected: set[str]) -> int | None:
    for h in hits:
        if page(h.source_uri) in expected:
            return h.rank
    return None


def summarize(ranks: list[int | None]) -> dict:
    n = len(ranks)
    return {
        "n": n,
        "hit@1": sum(r == 1 for r in ranks) / n,
        "hit@5": sum(r is not None and r <= 5 for r in ranks) / n,
        "mrr@10": sum(1 / r for r in ranks if r) / n,
    }


def main() -> int:
    questions = [json.loads(line) for line in QUESTIONS.read_text(encoding="utf-8").splitlines() if line.strip()]
    settings = Settings.from_env()
    embedder = SentenceTransformerEmbedder(settings.embedding_model, settings.embedding_dim)
    results: dict[str, dict] = {}
    per_question: dict[str, dict[str, int | None]] = defaultdict(dict)

    with db.create_pool(settings.database_url) as pool:
        searcher = Searcher(pool, embedder)
        searcher.search("warm up")
        for name, fusion in CONFIGS.items():
            ranks, groups = [], defaultdict(list)
            started = time.perf_counter()
            for q in questions:
                r = first_correct_rank(searcher.search(q["question"], top_k=DEPTH, fusion=fusion), set(q["expected"]))
                ranks.append(r)
                per_question[q["id"]][name] = r
                groups[f"category={q['category']}"].append(r)
                groups[f"phrasing={q['phrasing']}"].append(r)
                groups[f"origin={'random' if q['origin'].startswith('random') else 'targeted'}"].append(r)
            results[name] = {
                "overall": summarize(ranks),
                **{g: summarize(rs) for g, rs in sorted(groups.items())},
                "avg_latency_ms": (time.perf_counter() - started) / len(questions) * 1000,
            }

    print(f"## Retrieval eval - {len(questions)} questions, devportal corpus\n")
    print("| config | hit@1 | hit@5 | MRR@10 | avg latency |")
    print("|---|---|---|---|---|")
    for name, r in results.items():
        o = r["overall"]
        print(f"| {name} | {o['hit@1']:.0%} | {o['hit@5']:.0%} | {o['mrr@10']:.2f} | {r['avg_latency_ms']:.0f} ms |")

    groups = [k for k in next(iter(results.values())) if "=" in k]
    print("\n### hit@5 by slice\n")
    print("| slice | n | " + " | ".join(results) + " |")
    print("|---|---|" + "---|" * len(results))
    for g in groups:
        n = next(iter(results.values()))[g]["n"]
        print(f"| {g} | {n} | " + " | ".join(f"{r[g]['hit@5']:.0%}" for r in results.values()) + " |")

    print("\n### Per question (rank of first correct chunk, - = not in top 10)\n")
    print("| id | question | " + " | ".join(results) + " |")
    print("|---|---|" + "---|" * len(results))
    for q in questions:
        cells = [str(per_question[q["id"]][name] or "-") for name in results]
        print(f"| {q['id']} | {q['question'][:70]} | " + " | ".join(cells) + " |")

    json.dump({"results": results, "per_question": per_question}, sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
