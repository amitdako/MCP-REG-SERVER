"""Turn ingest inputs (text, file, directory, URL, GitHub repo) into Documents.

A single input becomes one Document. A directory or GitHub repo becomes a
Collection: a lazily-read sequence of Documents sharing a URI prefix, which is
what lets a re-ingest delete sources that disappeared upstream.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Literal

from rag_mcp import rst
from rag_mcp.document import DocFormat, format_for_path

log = logging.getLogger(__name__)

# Explicitly given files may be any text format; directory/repo scans take only
# documentation markup (the task: "the Markdown and RST content"), so files like
# requirements.txt or robots.txt in a repo are not ingested as documents.
TEXT_EXTENSIONS = {".md", ".markdown", ".mdx", ".rst", ".txt", ".text"}
DOC_EXTENSIONS = {".md", ".markdown", ".mdx", ".rst"}
SKIP_DIRS = {".git", ".github", "node_modules", "_build", ".venv", "__pycache__"}
MAX_DOCUMENT_BYTES = 5 * 1024 * 1024  # a single doc bigger than this is almost surely not docs
HTTP_TIMEOUT_S = 30
CLONE_TIMEOUT_S = 300

SourceType = Literal["file", "url", "text"]


class LoaderError(ValueError):
    """Bad input: reported back to the caller as a clear message."""


@dataclass(frozen=True)
class Document:
    source_uri: str
    source_type: SourceType
    text: str
    fmt: DocFormat
    title: str | None = None
    metadata: dict = field(default_factory=dict)


@dataclass
class Collection:
    description: str
    prefix: str  # every document's source_uri starts with this
    paths: list[Path]
    _load: Callable[[Path], Document]
    _cleanup: Callable[[], None] = lambda: None

    @property
    def total(self) -> int:
        return len(self.paths)

    def documents(self) -> Iterator[tuple[Path, Document | Exception]]:
        """Yields each file with its Document, or the error that loading it raised."""
        for path in self.paths:
            try:
                yield path, self._load(path)
            except Exception as exc:  # one bad file must not stop a 800-file ingest
                yield path, exc

    def cleanup(self) -> None:
        self._cleanup()


# ---------------------------------------------------------------- text

def from_text(text: str, name: str | None = None) -> Document:
    if not text.strip():
        raise LoaderError("text is empty")
    key = name.strip() if name and name.strip() else hashlib.sha256(text.encode()).hexdigest()[:16]
    # The name identifies the source (text:<name>); the title comes from the text's own heading.
    return Document(source_uri=f"text:{key}", source_type="text", text=text, fmt="markdown")


# ---------------------------------------------------------------- local files

def _read_text(path: Path) -> str:
    size = path.stat().st_size
    if size > MAX_DOCUMENT_BYTES:
        raise LoaderError(f"{path.name} is {size / 1e6:.1f} MB, limit is {MAX_DOCUMENT_BYTES / 1e6:.0f} MB")
    return path.read_text(encoding="utf-8", errors="replace")


def from_file(path: Path, root: Path | None = None, uri: str | None = None,
              metadata: dict | None = None) -> Document:
    if path.suffix.lower() not in TEXT_EXTENSIONS:
        raise LoaderError(f"unsupported file type {path.suffix!r}; supported: {sorted(TEXT_EXTENSIONS)}")
    text = _read_text(path)
    fmt = format_for_path(path.name)
    if fmt == "rst":
        text = rst.resolve_includes(text, path, root or path.parent)
    return Document(
        source_uri=uri or str(path.resolve()),
        source_type="file",
        text=text,
        fmt=fmt,
        metadata=metadata or {},
    )


def scan_directory(scan_root: Path, include_root: Path | None = None) -> list[Path]:
    """Documentation files under scan_root, minus files that other files include
    (those are inlined into their parent page instead of stored on their own)."""
    include_root = include_root or scan_root
    files = sorted(
        p for p in scan_root.rglob("*")
        if p.is_file() and p.suffix.lower() in DOC_EXTENSIONS
        and not SKIP_DIRS & set(p.relative_to(scan_root).parts)
    )
    included: set[Path] = set()
    for p in files:
        if p.suffix.lower() == ".rst":
            included.update(rst.include_targets(_read_text(p), p, include_root))
    return [p for p in files if p.resolve() not in included]


def from_directory(path: Path) -> Collection:
    root = path.resolve()
    paths = scan_directory(root)
    return Collection(
        description=f"directory {root}",
        prefix=f"{root}{os.sep}",
        paths=paths,
        _load=lambda p: from_file(p, root=root, metadata={"path": p.relative_to(root).as_posix()}),
    )


# ---------------------------------------------------------------- GitHub

_GITHUB = re.compile(
    r"^https?://github\.com/(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+?)(?:\.git)?"
    r"(?:/(?P<kind>tree|blob)/(?P<branch>[^/]+)(?:/(?P<subpath>.*?))?)?/?$"
)


@dataclass(frozen=True)
class GitHubRef:
    owner: str
    repo: str
    kind: str | None  # None (repo root), "tree" (directory) or "blob" (single file)
    branch: str | None
    subpath: str

    @property
    def clone_url(self) -> str:
        return f"https://github.com/{self.owner}/{self.repo}.git"

    def blob_url(self, branch: str, path: str) -> str:
        return f"https://github.com/{self.owner}/{self.repo}/blob/{branch}/{path}"

    def raw_url(self) -> str:
        return f"https://raw.githubusercontent.com/{self.owner}/{self.repo}/{self.branch}/{self.subpath}"


def parse_github_url(url: str) -> GitHubRef | None:
    m = _GITHUB.match(url.strip())
    if not m:
        return None
    return GitHubRef(m["owner"], m["repo"], m["kind"], m["branch"], (m["subpath"] or "").strip("/"))


def from_github(ref: GitHubRef) -> Collection:
    """Shallow-clone the repo and expose its docs (or one subdirectory) as a Collection."""
    tmp = Path(tempfile.mkdtemp(prefix="rag-clone-"))
    try:
        cmd = ["git", "clone", "--depth", "1", "--single-branch", "--quiet"]
        if ref.branch:
            cmd += ["--branch", ref.branch]
        subprocess.run(
            [*cmd, ref.clone_url, str(tmp / "repo")], check=True, capture_output=True, text=True,
            timeout=CLONE_TIMEOUT_S, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},  # never prompt: fail
        )
        repo_dir = tmp / "repo"
        branch = ref.branch or subprocess.run(
            ["git", "-C", str(repo_dir), "rev-parse", "--abbrev-ref", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
    except subprocess.CalledProcessError as exc:
        shutil.rmtree(tmp, ignore_errors=True)
        raise LoaderError(f"git clone of {ref.clone_url} failed: {exc.stderr.strip()[:300]}") from exc
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise

    scan_root = repo_dir / ref.subpath if ref.subpath else repo_dir
    if not scan_root.is_dir():
        shutil.rmtree(tmp, ignore_errors=True)
        raise LoaderError(f"{ref.subpath!r} is not a directory in {ref.owner}/{ref.repo}")

    def load(p: Path) -> Document:
        rel = p.relative_to(repo_dir).as_posix()
        return from_file(
            p, root=repo_dir, uri=ref.blob_url(branch, rel),
            metadata={"repo": f"{ref.owner}/{ref.repo}", "branch": branch, "path": rel},
        )

    prefix = ref.blob_url(branch, ref.subpath + "/" if ref.subpath else "")
    return Collection(
        description=f"github.com/{ref.owner}/{ref.repo}" + (f"/{ref.subpath}" if ref.subpath else ""),
        prefix=prefix,
        paths=scan_directory(scan_root, include_root=repo_dir),
        _load=load,
        _cleanup=lambda: shutil.rmtree(tmp, ignore_errors=True),
    )


# ---------------------------------------------------------------- URLs

def fetch_url(url: str, source_uri: str | None = None) -> Document:
    import httpx

    if not url.startswith(("http://", "https://")):
        raise LoaderError("only http(s) URLs are supported")
    try:
        with httpx.stream("GET", url, follow_redirects=True, timeout=HTTP_TIMEOUT_S,
                          headers={"User-Agent": "rag-mcp-server"}) as resp:
            resp.raise_for_status()
            body = bytearray()
            for part in resp.iter_bytes():
                body += part
                if len(body) > MAX_DOCUMENT_BYTES:
                    raise LoaderError(f"{url} is larger than {MAX_DOCUMENT_BYTES / 1e6:.0f} MB")
            content_type = resp.headers.get("content-type", "").lower()
            text = body.decode(resp.encoding or "utf-8", errors="replace")
    except httpx.HTTPError as exc:
        raise LoaderError(f"could not fetch {url}: {exc}") from exc

    path = PurePosixPath(httpx.URL(url).path)
    title = None
    if "html" in content_type or text.lstrip()[:15].lower().startswith(("<!doctype html", "<html")):
        title, text = html_to_markdown(text)
        fmt: DocFormat = "markdown"
    else:
        fmt = format_for_path(path.name)
    return Document(source_uri=source_uri or url, source_type="url", text=text, fmt=fmt, title=title)


_HTML_NOISE = ["script", "style", "noscript", "nav", "header", "footer", "aside", "form", "svg", "iframe"]


def html_to_markdown(html: str) -> tuple[str | None, str]:
    """Main content of a web page as Markdown (headings kept for structure-aware chunking)."""
    from bs4 import BeautifulSoup
    from markdownify import markdownify

    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(strip=True) if soup.title else None
    for tag in soup(_HTML_NOISE):
        tag.decompose()
    main = soup.find("main") or soup.find("article") or soup.body or soup
    markdown = markdownify(str(main), heading_style="ATX", bullets="-", strip=["img"])
    return title, re.sub(r"\n{3,}", "\n\n", markdown).strip()


# ---------------------------------------------------------------- dispatch

def resolve(*, text: str | None = None, path: str | None = None, url: str | None = None,
            name: str | None = None) -> Document | Collection:
    """Exactly one of text / path / url. Directories and GitHub repos become Collections."""
    given = [k for k, v in {"text": text, "path": path, "url": url}.items() if v]
    if len(given) != 1:
        raise LoaderError("provide exactly one of: text, path, url")

    if text:
        return from_text(text, name)

    if path:
        p = Path(path).expanduser()
        if p.is_dir():
            return from_directory(p)
        if p.is_file():
            return from_file(p)
        raise LoaderError(f"no such file or directory inside the server container: {path} "
                          f"(mount files under ./data and use /data/...)")

    gh = parse_github_url(url)
    if gh and gh.kind == "blob":
        return fetch_url(gh.raw_url(), source_uri=url.strip())
    if gh:
        return from_github(gh)
    return fetch_url(url)
