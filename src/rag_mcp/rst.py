"""Lightweight reStructuredText parser for Sphinx documentation.

We deliberately do not use docutils: the devportal uses Sphinx-only directives
and roles (grid, dropdown, :doc:, :bdg-secondary:, ...) that plain docutils
rejects. We only need two things from RST: where the headings are, and clean
readable text. Both are handled with small, well-tested heuristics.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from rag_mcp.document import RawSection

log = logging.getLogger(__name__)

# ---------------------------------------------------------------- includes

_INCLUDE = re.compile(r"^(?P<indent>[ \t]*)\.\.[ \t]+include::[ \t]*(?P<target>\S+)[ \t]*$")
_MAX_INCLUDE_DEPTH = 5


def resolve_includes(text: str, file_path: Path, root_dir: Path, _depth: int = 0) -> str:
    """Inline `.. include::` directives (textual inclusion, as Sphinx does).

    Absolute targets (`/includes/x.rst`) are relative to the docs root, others to
    the including file. Missing files and include cycles are logged and dropped.
    """
    out: list[str] = []
    for line in text.splitlines():
        m = _INCLUDE.match(line)
        if not m:
            out.append(line)
            continue
        target = m["target"]
        path = (root_dir / target.lstrip("/")) if target.startswith("/") else (file_path.parent / target)
        if _depth >= _MAX_INCLUDE_DEPTH:
            log.warning("include depth limit reached at %s -> %s", file_path, target)
            continue
        try:
            included = path.read_text(encoding="utf-8")
        except OSError:
            log.warning("cannot resolve include %s in %s", target, file_path)
            continue
        included = resolve_includes(included, path, root_dir, _depth + 1)
        indent = m["indent"]
        out.extend(indent + l if l.strip() else l for l in included.splitlines())
    return "\n".join(out)


# ---------------------------------------------------------------- headings

_PUNCT = set("!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~")


def _adornment_char(line: str) -> str | None:
    """'=====' -> '='. One punctuation char repeated >= 3 times, starting at column 0.

    Requiring a single repeated character (no spaces) is what keeps simple-table
    borders ('====  ====') and grid-table borders ('+----+') from being read as
    headings. Length vs. the title is not checked: real docs have sloppy underlines.
    """
    s = line.rstrip()
    if len(s) < 3 or s[0] not in _PUNCT or s != s[0] * len(s):
        return None
    return s[0]


def _is_title_text(line: str) -> bool:
    return (
        bool(line.strip())
        and not line[0].isspace()
        and _adornment_char(line) is None
        and not line.startswith("..")
    )


def split_sections(text: str) -> tuple[str, list[RawSection]]:
    """Split RST text into (preamble, sections). Bodies and titles are cleaned.

    Heading levels follow RST semantics: the order in which each adornment style
    (character + whether it has an overline) first appears in the document.
    """
    substitutions = _collect_substitutions(text)
    lines = text.splitlines()

    # (first line, line after the heading, title, style)
    headings: list[tuple[int, int, str, tuple[str, bool]]] = []
    i = 0
    while i < len(lines) - 1:
        char = _adornment_char(lines[i + 1])
        if char and _is_title_text(lines[i]):
            has_overline = i >= 1 and _adornment_char(lines[i - 1]) == char
            start = i - 1 if has_overline else i
            # A heading must follow a blank line (or start the document); otherwise
            # it is the last line of a paragraph followed by a transition.
            if start == 0 or not lines[start - 1].strip():
                headings.append((start, i + 2, lines[i].strip(), (char, has_overline)))
                i += 2
                continue
        i += 1

    styles: list[tuple[str, bool]] = []
    for *_, style in headings:
        if style not in styles:
            styles.append(style)

    preamble_end = headings[0][0] if headings else len(lines)
    preamble = clean_body("\n".join(lines[:preamble_end]), substitutions)
    sections: list[RawSection] = []
    for n, (_, body_start, title, style) in enumerate(headings):
        body_end = headings[n + 1][0] if n + 1 < len(headings) else len(lines)
        sections.append(
            RawSection(
                level=styles.index(style) + 1,
                title=clean_title(title, substitutions),
                body=clean_body("\n".join(lines[body_start:body_end]), substitutions),
            )
        )
    return preamble, sections


# ---------------------------------------------------------------- inline markup

_SUBST_DEF = re.compile(r"^\.\.[ \t]+\|(?P<name>[^|]+)\|[ \t]+replace::[ \t]*(?P<value>.*)$", re.M)
_SUBST_REF = re.compile(r"\|([^|\s][^|]*?)\|")
_ROLE_WITH_TARGET = re.compile(r":[\w:+.-]+:`([^`<]*?)\s*<[^`>]*>`")
_ROLE = re.compile(r":[\w:+.-]+:`([^`]*)`")
_LINK_WITH_TARGET = re.compile(r"`([^`<]+?)\s*<[^`>]+>`__?")
_LINK = re.compile(r"`([^`]+)`__?")
_LITERAL = re.compile(r"``(.+?)``")
_ESCAPE = re.compile(r"\\(.)")


def _collect_substitutions(text: str) -> dict[str, str]:
    subs = {m["name"]: m["value"].strip() for m in _SUBST_DEF.finditer(text)}
    return {name: clean_inline(value, {}) for name, value in subs.items()}


def clean_inline(text: str, substitutions: dict[str, str]) -> str:
    """RST inline markup -> readable, Markdown-flavoured text.

    :doc:`Kafka <...>` -> Kafka,  `site <https://...>`_ -> site,  ``x`` -> `x`
    """
    if substitutions:
        text = _SUBST_REF.sub(lambda m: substitutions.get(m[1], m[0]), text)
    text = _ROLE_WITH_TARGET.sub(r"\1", text)
    text = _ROLE.sub(r"\1", text)
    text = _LINK_WITH_TARGET.sub(r"\1", text)
    text = _LINK.sub(r"\1", text)
    text = _LITERAL.sub(r"`\1`", text)
    return _ESCAPE.sub(r"\1", text)


def clean_title(title: str, substitutions: dict[str, str] | None = None) -> str:
    return clean_inline(title, substitutions or {}).replace("`", "").replace("*", "").strip()


# ---------------------------------------------------------------- block markup

_DIRECTIVE = re.compile(r"^(?P<indent>[ \t]*)\.\.[ \t]+(?P<name>[A-Za-z][\w:-]*)::(?:[ \t]+(?P<arg>.*))?$")
_EXPLICIT_MARKUP = re.compile(r"^[ \t]*\.\.(?:[ \t]|$)")  # comments, labels, substitution defs
_OPTION = re.compile(r"^[ \t]+:[\w-]+:")
_GRID_BORDER = re.compile(r"^[ \t]*\+[-=+]+\+[ \t]*$")
_SIMPLE_TABLE_BORDER = re.compile(r"^[ \t]*=+(?:[ \t]+=+)+[ \t]*$")

# Pure presentation or navigation: no searchable facts.
_DROP_DIRECTIVES = {
    "image", "figure", "toctree", "tableofcontents", "contents", "raw", "mermaid",
    "highlight", "index", "meta", "literalinclude", "include",
}
_CODE_DIRECTIVES = {"code", "code-block", "sourcecode", "parsed-literal"}
_ADMONITIONS = {
    "note": "Note", "warning": "Warning", "tip": "Tip", "important": "Important",
    "caution": "Caution", "danger": "Danger", "attention": "Attention", "hint": "Hint",
    "error": "Error", "seealso": "See also",
}


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def _block_end(lines: list[str], start: int, parent_indent: int) -> int:
    """Index of the first line after an indented block that begins at `start`."""
    end = start
    while end < len(lines) and (not lines[end].strip() or _indent(lines[end]) > parent_indent):
        end += 1
    # Trailing blank lines belong to whatever follows.
    while end > start and not lines[end - 1].strip():
        end -= 1
    return end


def _code_fence(lines: list[str], language: str = "") -> list[str]:
    body = list(lines)
    while body and not body[0].strip():
        body.pop(0)
    if not body:
        return []
    common = min(_indent(l) for l in body if l.strip())
    return [f"```{language}", *(l[common:] for l in body), "```"]


def clean_body(text: str, substitutions: dict[str, str]) -> str:
    """Turn an RST section body into clean text: keep prose, code, tables and
    admonitions; drop images, TOCs, comments, labels and table decoration."""
    lines = text.splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]

        directive = _DIRECTIVE.match(line)
        if directive:
            indent = len(directive["indent"])
            name = directive["name"].lower()
            arg = (directive["arg"] or "").strip()
            end = _block_end(lines, i + 1, indent)
            content_start = i + 1
            while content_start < end and _OPTION.match(lines[content_start]):
                content_start += 1
            if name in _DROP_DIRECTIVES:
                i = end
            elif name in _CODE_DIRECTIVES:
                out.extend(_code_fence(lines[content_start:end], arg.split()[0] if arg else ""))
                i = end
            else:
                # Admonitions and containers (note, dropdown, grid, list-table, topic, ...):
                # keep a short label and let the content flow through normal processing.
                label = _ADMONITIONS.get(name)
                header = f"{label}: {arg}".strip() if label else arg
                if header:
                    out.append(clean_inline(header, substitutions).strip())
                i = content_start
            continue

        if _EXPLICIT_MARKUP.match(line):
            i = _block_end(lines, i + 1, _indent(line))
            continue

        if line.strip() == ":orphan:" or _GRID_BORDER.match(line) or _SIMPLE_TABLE_BORDER.match(line):
            i += 1
            continue

        # Literal block: a paragraph ending with '::' followed by an indented block.
        stripped = line.rstrip()
        if stripped.endswith("::"):
            nxt = i + 1
            while nxt < len(lines) and not lines[nxt].strip():
                nxt += 1
            if nxt < len(lines) and _indent(lines[nxt]) > _indent(line):
                intro = stripped[:-2].rstrip()
                if intro:
                    intro_text = intro if stripped.endswith(" ::") else intro + ":"
                    out.append(_clean_line(intro_text, substitutions))
                end = _block_end(lines, nxt, _indent(line))
                out.extend(_code_fence(lines[nxt:end]))
                i = end
                continue

        out.append(_clean_line(line, substitutions))
        i += 1

    return _squeeze_blank_lines("\n".join(out))


def _clean_line(line: str, substitutions: dict[str, str]) -> str:
    return re.sub(r"[ \t]{2,}", " ", clean_inline(line, substitutions)).strip()


def _squeeze_blank_lines(text: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", text).strip()
