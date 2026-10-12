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


def _include_path(target: str, file_path: Path, root_dir: Path) -> Path:
    return (root_dir / target.lstrip("/")) if target.startswith("/") else (file_path.parent / target)


def include_targets(text: str, file_path: Path, root_dir: Path) -> list[Path]:
    """Files this document pulls in with `.. include::` (not recursive)."""
    return [
        _include_path(m["target"], file_path, root_dir).resolve()
        for m in map(_INCLUDE.match, text.splitlines()) if m
    ]


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
        path = _include_path(target, file_path, root_dir)
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
# Source typo seen in the devportal: `top`` (one backtick to open, two to close).
# One word only: anything longer would pair the end of a link with a real ``literal``.
_HALF_LITERAL = re.compile(r"(?<![`\w])`([^`\s]+)``(?![`\w])")
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
    text = _HALF_LITERAL.sub(r"``\1``", text)
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
            elif name == "list-table":
                if arg:
                    out.append(clean_inline(arg, substitutions).strip())
                out.extend(_list_table(lines[i + 1:content_start], lines[content_start:end], substitutions))
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

    return _squeeze_blank_lines(_join_wrapped_links("\n".join(out)))


_ROW_START = re.compile(r"^(?P<indent>[ \t]*)\*[ \t]+-(?:[ \t]+(?P<text>.*))?$")
_CELL_START = re.compile(r"^(?P<indent>[ \t]*)-(?:[ \t]+(?P<text>.*))?$")


def _list_table(options: list[str], body: list[str], substitutions: dict[str, str]) -> list[str]:
    """`.. list-table::` -> one line per row, each cell labelled with its column header:

        - Parameter: `admin_username` · Value Type: string · Description: Custom ...

    A row stays self-contained, so it still reads correctly when a long table is
    split across chunks (a Markdown table would lose its header row there).
    """
    header_rows = 0
    for line in options:
        m = re.match(r"^[ \t]+:header-rows:[ \t]*(\d+)", line)
        if m:
            header_rows = int(m[1])

    rows: list[list[list[str]]] = []  # row -> cell -> lines of text
    row_indent = 0
    for line in body:
        if not line.strip():
            continue
        row = _ROW_START.match(line)
        cell = _CELL_START.match(line)
        if row:
            row_indent = len(row["indent"])
            rows.append([[row["text"] or ""]])
        elif cell and rows and len(cell["indent"]) == row_indent + 2:
            rows[-1].append([cell["text"] or ""])
        elif rows:
            rows[-1][-1].append(line.strip())  # cell continuation, incl. nested lists

    cells = [[_clean_line(" ".join(part for part in cell if part), substitutions) for cell in row]
             for row in rows]
    headers = cells[header_rows - 1] if 0 < header_rows <= len(cells) else []
    out = []
    for row in cells[header_rows:]:
        parts = [f"{headers[n]}: {text}" if n < len(headers) and headers[n] else text
                 for n, text in enumerate(row) if text]
        if parts:
            out.append("- " + " · ".join(parts))
    return out


def _join_wrapped_links(text: str) -> str:
    """Clean links and roles that wrap onto a second line, e.g.

        the `Aiven
        Client <https://github.com/aiven/aiven-client>`__ .

    clean_inline works per line and never sees them whole. Applied per paragraph
    and outside code fences, so a stray backtick cannot pair with a far-away link.
    """
    def clean(paragraph: str) -> str:
        if "\n" not in paragraph:
            return paragraph
        one_line = lambda m: re.sub(r"\s*\n\s*", " ", m[1])
        paragraph = _ROLE_WITH_TARGET.sub(one_line, paragraph)
        return _LINK_WITH_TARGET.sub(one_line, paragraph)

    parts = re.split(r"(^```.*?^```[ \t]*$)", text, flags=re.M | re.S)
    return "".join(
        part if part.startswith("```") else "\n\n".join(clean(p) for p in part.split("\n\n"))
        for part in parts
    )


def _clean_line(line: str, substitutions: dict[str, str]) -> str:
    return re.sub(r"[ \t]{2,}", " ", clean_inline(line, substitutions)).strip()


def _squeeze_blank_lines(text: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", text).strip()
