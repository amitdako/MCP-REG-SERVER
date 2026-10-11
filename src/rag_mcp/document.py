"""Format-independent document model: a tree of headed sections.

The RST and Markdown parsers both produce a flat list of RawSection (heading
level + title + body text). build_tree turns that into a tree, which is what
the chunker walks: the tree is what lets us keep a breadcrumb per chunk and
never pack text from unrelated parts of a page together.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import PurePath
from typing import Literal

DocFormat = Literal["rst", "markdown"]


@dataclass(frozen=True)
class RawSection:
    level: int  # 1 = top. Only relative order matters; levels may skip.
    title: str
    body: str


@dataclass
class Node:
    title: str | None  # None only for the root
    body: str  # text directly under this heading, before any sub-heading
    path: tuple[str, ...]  # titles from the top of the document down to this node
    children: list[Node] = field(default_factory=list)

    @property
    def depth(self) -> int:
        return len(self.path)

    def walk(self):
        yield self
        for child in self.children:
            yield from child.walk()


def build_tree(preamble: str, sections: list[RawSection]) -> Node:
    """Nest each section under the closest preceding section with a lower level."""
    root = Node(title=None, body=preamble, path=())
    stack: list[tuple[int, Node]] = [(0, root)]
    for section in sections:
        while stack[-1][0] >= section.level:
            stack.pop()
        parent = stack[-1][1]
        node = Node(title=section.title, body=section.body, path=parent.path + (section.title,))
        parent.children.append(node)
        stack.append((section.level, node))
    return root


def document_title(root: Node) -> str | None:
    for node in root.walk():
        if node.title:
            return node.title
    return None


def format_for_path(path: str) -> DocFormat:
    """Pick a parser by file extension. Plain text goes through the Markdown parser:
    with no `#` headings it simply becomes one section."""
    return "rst" if PurePath(path).suffix.lower() in {".rst", ".rest"} else "markdown"


def parse_document(text: str, fmt: DocFormat) -> Node:
    # Imported here to avoid a cycle: the parsers import RawSection from this module.
    if fmt == "rst":
        from rag_mcp import rst

        preamble, sections = rst.split_sections(text)
    else:
        from rag_mcp import markdown

        preamble, sections = markdown.split_sections(text)
    return build_tree(preamble, sections)
