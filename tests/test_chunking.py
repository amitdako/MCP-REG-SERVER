"""Chunking, measured with the real bge-small tokenizer.

The guarantees retrieval depends on:
  * no chunk ever exceeds the model limit (anything beyond is silently not embedded),
  * no text is lost,
  * small sections are packed, but never across a parent boundary,
  * long sections are split with overlap and stay readable (heading, code fences).
"""

from __future__ import annotations


import pytest

from rag_mcp.chunking import ChunkerConfig, chunk_tree
from rag_mcp.document import parse_document
from rag_mcp.tokens import TokenCounter

pytest.importorskip("transformers")

MODEL_LIMIT = 510  # 512 minus [CLS]/[SEP]
CONFIG = ChunkerConfig()


@pytest.fixture(scope="session")
def counter() -> TokenCounter:
    return TokenCounter.from_pretrained("BAAI/bge-small-en-v1.5")


def chunks_of(text: str, counter: TokenCounter, fmt="markdown", config=CONFIG):
    return chunk_tree(parse_document(text, fmt), counter, config)


def assert_within_limits(chunks, counter):
    for c in chunks:
        assert c.token_count == counter.count(c.embedding_input)
        assert c.token_count <= MODEL_LIMIT
        assert c.content.strip()


def words(n: int, tag: str) -> str:
    return " ".join(f"{tag}{i}" for i in range(n))


# ---------------------------------------------------------------- small docs

def test_small_document_is_one_chunk_with_breadcrumb(counter):
    chunks = chunks_of("# Kafka\n\nIntro.\n\n## Limits\n\nMax 100 topics per service.\n", counter)
    assert len(chunks) == 1
    c = chunks[0]
    assert c.heading_path == ("Kafka",)
    assert c.embedding_input.startswith("[Kafka]\n")
    assert "[Kafka" not in c.content  # breadcrumb only in what is embedded
    assert "## Limits" in c.content and "Max 100 topics" in c.content


def test_heading_without_text_produces_no_chunk(counter):
    assert chunks_of("# Empty\n\n## Also empty\n", counter) == []


# ---------------------------------------------------------------- packing

def test_small_sibling_sections_are_packed(counter):
    # Like devportal config params: many ~20-token sections under one page.
    params = "\n\n".join(f"## param_{i}\n\n*boolean*\n\nEnables feature number {i}." for i in range(60))
    chunks = chunks_of(f"# Advanced parameters\n\n{params}\n", counter)

    assert 2 <= len(chunks) < 15  # packed, not 60 tiny chunks
    for c in chunks:
        assert c.token_count <= CONFIG.target_tokens + CONFIG.breadcrumb_max_tokens
        assert c.heading_path == ("Advanced parameters",)
    # Every parameter heading lands in exactly one chunk.
    for i in range(60):
        assert sum(f"## param_{i}\n" in c.content for c in chunks) == 1


def test_packing_never_crosses_a_parent_boundary(counter):
    # `private` is too big to be one chunk, so its children are packed among themselves;
    # they must not be packed together with the following `public` section.
    private_children = "\n\n".join(f"### p{i}\n\n{words(60, 'priv')}" for i in range(8))
    text = f"# Page\n\n## private\n\n{private_children}\n\n## public\n\nPublic access text.\n"
    chunks = chunks_of(text, counter)
    for c in chunks:
        assert not ("priv" in c.content and "Public access text" in c.content)
    public = [c for c in chunks if "Public access text" in c.content]
    assert public[0].heading_path == ("Page", "public")


def test_no_text_is_lost(counter):
    paras = [f"Unique paragraph {i}: " + words(40, f"w{i}x") for i in range(40)]
    text = "# Doc\n\n" + "\n\n".join(
        f"## S{i}\n\n" + "\n\n".join(paras[i * 4:(i + 1) * 4]) for i in range(10)
    )
    joined = "\n".join(c.content for c in chunks_of(text, counter))
    for p in paras:
        assert p in joined


# ---------------------------------------------------------------- long sections

def test_long_section_is_split_with_overlap_and_heading(counter):
    paras = [f"Step {i}. " + words(30, f"s{i}x") for i in range(40)]  # ~2000 tokens
    chunks = chunks_of("# Guide\n\n## Install\n\n" + "\n\n".join(paras), counter)

    assert len(chunks) > 3
    assert_within_limits(chunks, counter)
    for c in chunks:
        assert c.content.startswith("## Install\n")  # each piece keeps its heading
        assert c.heading_path == ("Guide", "Install")
    # Overlap: each piece starts with the tail end of the previous one (~overlap_tokens).
    for a, b in zip(chunks, chunks[1:]):
        carried = b.content.removeprefix("## Install\n\n").split("\n\n")[0]
        assert carried in a.content
        assert 10 <= counter.count(carried) <= CONFIG.overlap_tokens


def test_huge_single_line_is_split_on_token_boundaries(counter):
    text = "# Metrics\n\n" + words(3000, "metric_name_")  # no newlines at all
    chunks = chunks_of(text, counter)
    assert len(chunks) > 5
    assert_within_limits(chunks, counter)


def test_split_code_block_keeps_fences_balanced(counter):
    code = "\n".join(f"cpu_usage_metric_{i} gauge" for i in range(400))
    chunks = chunks_of(f"# Prometheus metrics\n\n```shell\n{code}\n```\n", counter)
    assert len(chunks) > 1
    assert_within_limits(chunks, counter)
    for c in chunks:
        assert c.content.count("```") % 2 == 0


# ---------------------------------------------------------------- breadcrumbs + config

def test_deep_long_breadcrumb_is_shortened_from_the_middle(counter):
    levels = [f"Very long heading title number {i} with many words in it" for i in range(1, 7)]
    text = "\n\n".join(f"{'#' * (i + 1)} {t}" for i, t in enumerate(levels)) + "\n\nLeaf text.\n"
    chunks = chunks_of(text, counter)
    crumb = chunks[0].embedding_input.split("\n", 1)[0]
    assert counter.count(crumb) <= CONFIG.breadcrumb_max_tokens
    assert crumb.startswith("[" + levels[0]) and "..." in crumb
    assert chunks[0].heading_path[-1] == levels[-1]  # full path still stored


def test_rst_document_end_to_end(counter):
    text = (
        "Advanced parameters\n===================\n\n"
        + "\n\n".join(f"``param_{i}``\n{'-' * 12}\n*integer*\n\nLimit number {i}." for i in range(30))
    )
    chunks = chunks_of(text, counter, fmt="rst")
    assert_within_limits(chunks, counter)
    joined = "\n".join(c.content for c in chunks)
    assert "## param_29" in joined and "Limit number 29." in joined


def test_config_must_fit_the_model():
    with pytest.raises(ValueError):
        ChunkerConfig(max_tokens=500, breadcrumb_max_tokens=48)
    with pytest.raises(ValueError):
        ChunkerConfig(target_tokens=500, max_tokens=450)
