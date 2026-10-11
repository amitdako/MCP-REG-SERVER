"""Input handling: what becomes a source, under which URI, and what is skipped."""

from __future__ import annotations

import http.server
import threading
from pathlib import Path

import pytest

from rag_mcp import loaders
from rag_mcp.loaders import Collection, Document, LoaderError


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------- directories

def test_directory_scan_skips_included_files_and_noise_dirs(tmp_path):
    write(tmp_path / "docs/page.rst", "Page\n====\n\n.. include:: /includes/params.rst\n")
    write(tmp_path / "includes/params.rst", "``ip_filter``\n----\n\nCIDR list.\n")
    write(tmp_path / "includes/unused.rst", "Standalone\n==========\n\ntext\n")
    write(tmp_path / ".github/ISSUE_TEMPLATE.md", "# template\n")
    write(tmp_path / "docs/notes.md", "# Notes\n")
    write(tmp_path / "docs/image.png", "not text")

    found = {p.relative_to(tmp_path).as_posix() for p in loaders.scan_directory(tmp_path)}

    # params.rst is inlined into page.rst; an include file nobody includes is still a source.
    assert found == {"docs/page.rst", "docs/notes.md", "includes/unused.rst"}


def test_directory_documents_have_includes_resolved(tmp_path):
    write(tmp_path / "docs/page.rst", "Page\n====\n\n.. include:: /includes/params.rst\n")
    write(tmp_path / "includes/params.rst", "CIDR list.\n")
    collection = loaders.resolve(path=str(tmp_path))
    assert isinstance(collection, Collection)
    [(_, doc)] = list(collection.documents())
    assert "CIDR list." in doc.text
    assert doc.source_uri.startswith(collection.prefix)
    assert doc.metadata["path"] == "docs/page.rst"


def test_bad_file_is_reported_not_raised(tmp_path, monkeypatch):
    write(tmp_path / "a.md", "# A\n")
    write(tmp_path / "b.md", "# B\n")
    monkeypatch.setattr(loaders, "MAX_DOCUMENT_BYTES", 3)  # every file is now "too big"
    results = list(loaders.resolve(path=str(tmp_path)).documents())
    assert len(results) == 2 and all(isinstance(r, LoaderError) for _, r in results)


# ---------------------------------------------------------------- single inputs

def test_text_input_uri_is_stable():
    a = loaders.from_text("hello", name="notes")
    assert a.source_uri == "text:notes" and a.source_type == "text"
    # Without a name, identical text maps to the same source (re-ingest is idempotent).
    assert loaders.from_text("same").source_uri == loaders.from_text("same").source_uri


@pytest.mark.parametrize("kwargs", [{}, {"text": "a", "url": "https://x"}])
def test_exactly_one_input_required(kwargs):
    with pytest.raises(LoaderError):
        loaders.resolve(**kwargs)


def test_unsupported_file_type_is_rejected(tmp_path):
    with pytest.raises(LoaderError, match="unsupported"):
        loaders.resolve(path=str(write(tmp_path / "x.pdf", "%PDF")))


def test_missing_path_explains_container_paths():
    with pytest.raises(LoaderError, match="/data"):
        loaders.resolve(path="/definitely/not/here.md")


# ---------------------------------------------------------------- GitHub URLs

@pytest.mark.parametrize("url, expected", [
    ("https://github.com/aiven/devportal", ("aiven", "devportal", None, None, "")),
    ("https://github.com/aiven/devportal.git", ("aiven", "devportal", None, None, "")),
    ("https://github.com/aiven/devportal/tree/main/docs/products/kafka",
     ("aiven", "devportal", "tree", "main", "docs/products/kafka")),
    ("https://github.com/aiven/devportal/blob/main/docs/a.rst", ("aiven", "devportal", "blob", "main", "docs/a.rst")),
])
def test_github_url_parsing(url, expected):
    ref = loaders.parse_github_url(url)
    assert (ref.owner, ref.repo, ref.kind, ref.branch, ref.subpath) == expected


def test_github_file_uri_is_a_clickable_blob_url():
    ref = loaders.parse_github_url("https://github.com/aiven/devportal")
    assert ref.blob_url("main", "docs/a.rst") == "https://github.com/aiven/devportal/blob/main/docs/a.rst"
    assert loaders.parse_github_url("https://example.com/aiven/devportal") is None


# ---------------------------------------------------------------- HTML + URLs

HTML_PAGE = """<!doctype html><html><head><title>Kafka limits</title><script>var x=1;</script></head>
<body><nav>Home | Products</nav><main>
<h1>Kafka limits</h1><p>Intro text.</p>
<h2>Topics</h2><p>Max <code>1000</code> topics.</p><ul><li>one</li></ul>
</main><footer>Copyright</footer></body></html>"""


def test_html_to_markdown_keeps_structure_drops_chrome():
    title, md = loaders.html_to_markdown(HTML_PAGE)
    assert title == "Kafka limits"
    assert "# Kafka limits" in md and "## Topics" in md and "`1000`" in md
    for noise in ("Home | Products", "Copyright", "var x"):
        assert noise not in md


@pytest.fixture
def http_server(tmp_path):
    write(tmp_path / "page.html", HTML_PAGE)
    write(tmp_path / "guide.rst", "Guide\n=====\n\nRST body.\n")
    handler = lambda *a, **kw: http.server.SimpleHTTPRequestHandler(*a, directory=str(tmp_path), **kw)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def test_fetch_html_and_raw_files(http_server):
    page = loaders.resolve(url=f"{http_server}/page.html")
    assert isinstance(page, Document) and page.source_type == "url"
    assert page.fmt == "markdown" and "## Topics" in page.text and page.title == "Kafka limits"

    raw = loaders.resolve(url=f"{http_server}/guide.rst")
    assert raw.fmt == "rst" and "RST body." in raw.text


def test_fetch_errors_are_loader_errors(http_server):
    with pytest.raises(LoaderError):
        loaders.resolve(url=f"{http_server}/missing.md")
