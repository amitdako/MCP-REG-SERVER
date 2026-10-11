"""Minimal Markdown section splitter (ATX `#` headings, fenced code aware)."""

from __future__ import annotations

import re

from rag_mcp.document import RawSection

_ATX = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
_FENCE = re.compile(r"^[ \t]*(```|~~~)")
_FRONT_MATTER = re.compile(r"\A\s*---[ \t]*\n.*?\n---[ \t]*(\n|\Z)", re.S)
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)


def split_sections(text: str) -> tuple[str, list[RawSection]]:
    text = _HTML_COMMENT.sub("", _FRONT_MATTER.sub("", text))
    preamble: list[str] = []
    sections: list[tuple[int, str, list[str]]] = []
    fence: str | None = None

    for line in text.splitlines():
        fence_match = _FENCE.match(line)
        if fence_match:
            marker = fence_match[1]
            fence = None if fence == marker else (fence or marker)
        heading = None if fence else _ATX.match(line)
        if heading:
            sections.append((len(heading[1]), _clean_title(heading[2]), []))
        elif sections:
            sections[-1][2].append(line)
        else:
            preamble.append(line)

    return _squeeze("\n".join(preamble)), [
        RawSection(level=level, title=title, body=_squeeze("\n".join(body)))
        for level, title, body in sections
    ]


def _clean_title(title: str) -> str:
    # Only backticks and asterisks: underscores are part of identifiers like max_connections.
    return re.sub(r"(`+|\*{1,2})(.+?)\1", r"\2", title).strip()


def _squeeze(text: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", text).strip()
