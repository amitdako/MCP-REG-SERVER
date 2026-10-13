"""Structure-aware, token-based chunking of a section tree.

Rules (see docs/ARCHITECTURE.md, 2.1 step 4):
  * A section whose whole subtree fits in `target_tokens` becomes one piece.
  * Pieces that are whole subtrees of siblings (plus their parent's intro text)
    are packed together up to `target_tokens` - never across a parent boundary.
  * A single section longer than `max_tokens` is split on paragraphs, then lines,
    then tokens, with `overlap_tokens` of context carried between its pieces.
  * Every chunk gets a breadcrumb ("[Page > Section > Sub]") prepended to the text
    that is embedded; the content returned to Claude stays clean.
  * Hard guarantee: breadcrumb + content always fits the embedding model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace

from rag_mcp.document import Node
from rag_mcp.tokens import TokenCounter

# Part of the content hash: bump it whenever chunking output changes - including
# changes in the parsers (rst.py, markdown.py) - so re-ingesting re-chunks and
# re-embeds instead of skipping "unchanged" sources.
CHUNKER_VERSION = "2"

_SPECIAL_TOKENS = 2  # [CLS] and [SEP], added by the model on top of our text


@dataclass(frozen=True)
class ChunkerConfig:
    target_tokens: int = 350
    max_tokens: int = 450
    overlap_tokens: int = 50
    breadcrumb_max_tokens: int = 48
    model_max_tokens: int = 512

    def __post_init__(self) -> None:
        if not 0 <= self.overlap_tokens < self.target_tokens <= self.max_tokens:
            raise ValueError("need 0 <= overlap < target <= max")
        if self.max_tokens + self.breadcrumb_max_tokens + _SPECIAL_TOKENS > self.model_max_tokens:
            raise ValueError("max_tokens + breadcrumb_max_tokens must fit in the model limit")


@dataclass(frozen=True)
class Chunk:
    heading_path: tuple[str, ...]
    content: str  # clean text for Claude
    embedding_input: str  # breadcrumb + content: what gets embedded and keyword-indexed
    token_count: int  # tokens of embedding_input (without special tokens)


@dataclass(frozen=True)
class _Piece:
    path: tuple[str, ...]
    text: str
    tokens: int
    packable: bool


def chunk_tree(root: Node, counter: TokenCounter, config: ChunkerConfig = ChunkerConfig()) -> list[Chunk]:
    return [_finalize(piece, counter, config) for piece in _Chunker(counter, config).pieces(root)]


class _Chunker:
    def __init__(self, counter: TokenCounter, config: ChunkerConfig) -> None:
        self.counter = counter
        self.config = config

    def pieces(self, node: Node) -> list[_Piece]:
        whole = _subtree_text(node)
        if not whole:
            return []
        tokens = self.counter.count(whole)
        if tokens <= self.config.target_tokens:
            return [_Piece(_content_path(node), whole, tokens, packable=True)]

        items: list[_Piece] = []
        own = _render(node) if node.body.strip() else ""  # a bare heading is not a piece
        if own:
            own_tokens = self.counter.count(own)
            if own_tokens <= self.config.max_tokens:
                items.append(_Piece(node.path, own, own_tokens, own_tokens <= self.config.target_tokens))
            else:
                items.extend(self._split_long(node))
        for child in node.children:
            child_pieces = self.pieces(child)
            if len(child_pieces) == 1 and child_pieces[0].packable:
                items.extend(child_pieces)  # a whole child subtree: may pack with siblings
            else:
                # Already packed inside the child; must not mix with this level.
                items.extend(_Piece(p.path, p.text, p.tokens, packable=False) for p in child_pieces)
        return self._pack(items)

    def _pack(self, items: list[_Piece]) -> list[_Piece]:
        out: list[_Piece] = []
        run: list[_Piece] = []

        def flush() -> None:
            if run:
                out.append(_merge(run))
                run.clear()

        for item in items:
            if not item.packable:
                flush()
                out.append(item)
                continue
            if run and sum(p.tokens for p in run) + item.tokens > self.config.target_tokens:
                flush()
            run.append(item)
        flush()
        return out

    def _split_long(self, node: Node) -> list[_Piece]:
        """Split one oversized section; each piece repeats the section heading."""
        heading = _heading_line(node)
        budget = self.config.target_tokens - (self.counter.count(heading) if heading else 0)
        units = self._units(node.body, budget)

        groups: list[list[_Unit]] = []
        current: list[_Unit] = []
        current_tokens = 0
        for unit in units:
            if current and current_tokens + unit.tokens > budget:
                groups.append(current)
                current = self._overlap(current)
                current_tokens = sum(u.tokens for u in current)
                if current_tokens + unit.tokens > budget:  # no room for overlap
                    current, current_tokens = [], 0
            current.append(unit)
            current_tokens += unit.tokens
        if current:
            groups.append(current)

        pieces = []
        for group in groups:
            body = group[0].text + "".join(u.joiner + u.text for u in group[1:])
            # Keep code fences balanced when a code block is cut between pieces.
            if group[0].in_fence:
                body = "```\n" + body
            if group[-1].in_fence ^ (_fence_toggles(group[-1].text) % 2 == 1):
                body += "\n```"
            text = f"{heading}\n\n{body}" if heading else body
            pieces.append(_Piece(node.path, text, self.counter.count(text), packable=False))
        return pieces

    def _overlap(self, group: list[_Unit]) -> list[_Unit]:
        """Context carried into the next piece: trailing whole units up to
        overlap_tokens; if even the last unit is bigger, the end of that unit."""
        tail: list[_Unit] = []
        total = 0
        for unit in reversed(group):
            if total + unit.tokens > self.config.overlap_tokens:
                break
            tail.insert(0, unit)
            total += unit.tokens
        if tail or not self.config.overlap_tokens:
            return tail
        last = group[-1]
        text = self.counter.tail(last.text, self.config.overlap_tokens)
        if not text:
            return []
        cut_toggles = _fence_toggles(last.text) - _fence_toggles(text)
        return [_Unit(text, self.counter.count(text), last.joiner, last.in_fence ^ (cut_toggles % 2 == 1))]

    def _units(self, body: str, budget: int) -> list[_Unit]:
        """Paragraphs; oversized paragraphs become lines; oversized lines become token windows."""
        units: list[_Unit] = []
        for para in re.split(r"\n[ \t]*\n", body.strip()):
            n = self.counter.count(para)
            if n <= budget:
                units.append(_Unit(para, n, "\n\n"))
                continue
            for i, line in enumerate(para.split("\n")):
                joiner = "\n\n" if i == 0 else "\n"
                n = self.counter.count(line)
                if n <= budget:
                    units.append(_Unit(line, n, joiner))
                    continue
                for j, part in enumerate(self.counter.split(line, budget)):
                    units.append(_Unit(part, self.counter.count(part), joiner if j == 0 else " "))
        # Record whether each unit starts inside a ``` code fence.
        inside = False
        for i, unit in enumerate(units):
            units[i] = replace(unit, in_fence=inside)
            inside ^= _fence_toggles(unit.text) % 2 == 1
        return units


@dataclass(frozen=True)
class _Unit:
    text: str
    tokens: int
    joiner: str  # separator placed before this unit when it is not first in a piece
    in_fence: bool = False  # starts inside a ``` code block


def _heading_line(node: Node) -> str:
    return f"{'#' * min(node.depth, 6)} {node.title}" if node.title else ""


def _render(node: Node) -> str:
    return "\n\n".join(part for part in (_heading_line(node), node.body.strip()) if part)


def _subtree_text(node: Node) -> str:
    """The node and all descendants, in document order. Empty if there is no body
    text anywhere (a heading alone carries no answer)."""
    if not any(n.body.strip() for n in node.walk()):
        return ""
    return "\n\n".join(part for part in (_render(n) for n in node.walk()) if part)


def _content_path(node: Node) -> tuple[str, ...]:
    """Deepest path that still covers all text in the subtree: a document whose only
    text sits under '# Kafka' gets the breadcrumb [Kafka], not an empty one."""
    return _common_prefix([n.path for n in node.walk() if n.body.strip()])


def _merge(run: list[_Piece]) -> _Piece:
    if len(run) == 1:
        return run[0]
    return _Piece(
        path=_common_prefix([p.path for p in run]),
        text="\n\n".join(p.text for p in run),
        tokens=sum(p.tokens for p in run),
        packable=True,
    )


def _common_prefix(paths: list[tuple[str, ...]]) -> tuple[str, ...]:
    prefix = paths[0]
    for path in paths[1:]:
        n = 0
        while n < min(len(prefix), len(path)) and prefix[n] == path[n]:
            n += 1
        prefix = prefix[:n]
    return prefix


def _fence_toggles(text: str) -> int:
    return sum(1 for line in text.splitlines() if line.lstrip().startswith("```"))


def _breadcrumb(path: tuple[str, ...], counter: TokenCounter, config: ChunkerConfig) -> str:
    """'[Page > Section > Sub]'. Too long -> drop middle levels: '[Page > ... > Sub]'."""
    if not path:
        return ""
    parts = list(path)
    for keep in range(len(parts) - 1, 0, -1):
        candidate = parts[:1] + (["..."] if keep < len(parts) - 1 else []) + parts[-keep:]
        text = "[" + " > ".join(candidate) + "]"
        if counter.count(text) <= config.breadcrumb_max_tokens:
            return text
    # Even "[Page]" is too long (absurd title): hard cut on token boundary.
    return counter.split("[" + parts[0], config.breadcrumb_max_tokens - 1)[0] + "]"


def _finalize(piece: _Piece, counter: TokenCounter, config: ChunkerConfig) -> Chunk:
    breadcrumb = _breadcrumb(piece.path, counter, config)
    embedding_input = f"{breadcrumb}\n{piece.text}" if breadcrumb else piece.text
    tokens = counter.count(embedding_input)
    limit = config.model_max_tokens - _SPECIAL_TOKENS
    if tokens > limit:  # defensive: the rules above should make this unreachable
        raise AssertionError(f"chunk has {tokens} tokens, limit {limit}: {piece.path}")
    return Chunk(piece.path, piece.text, embedding_input, tokens)
