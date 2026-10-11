from __future__ import annotations

from textwrap import dedent

from rag_mcp import markdown
from rag_mcp.document import format_for_path, parse_document


def test_headings_and_nesting():
    text = dedent("""
        ---
        title: front matter is dropped
        ---
        Intro.

        # Guide

        ## Install

        Run it.

        ### Linux

        apt.

        ## Configure `max_connections`

        Set it.
        """)
    preamble, secs = markdown.split_sections(text)
    assert preamble == "Intro."
    assert [(s.level, s.title) for s in secs] == [
        (1, "Guide"), (2, "Install"), (3, "Linux"), (2, "Configure max_connections"),
    ]
    root = parse_document(text, "markdown")
    assert root.children[0].children[0].children[0].path == ("Guide", "Install", "Linux")


def test_hash_lines_inside_code_fences_are_not_headings():
    text = "# Real\n\n```bash\n# not a heading\nls\n```\n\n~~~\n# also not\n~~~\n"
    _, secs = markdown.split_sections(text)
    assert [s.title for s in secs] == ["Real"]
    assert "# not a heading" in secs[0].body


def test_plain_text_without_headings_is_one_preamble():
    preamble, secs = markdown.split_sections("just some pasted text\n\nwith two paragraphs")
    assert secs == [] and "two paragraphs" in preamble


def test_format_detection():
    assert format_for_path("docs/a.rst") == "rst"
    assert format_for_path("README.md") == "markdown"
    assert format_for_path("notes.txt") == "markdown"
