# RAG MCP Server — Architecture & Technical Specifications

An MCP server (stdio transport, Docker) that lets Claude ingest documentation and answer
questions over it with retrieval-augmented generation. Reference knowledge base: the full
[aiven/devportal](https://github.com/aiven/devportal) repository.

| | |
|---|---|
| **Corpus** | 814 documents → 2,907 chunks, 762k tokens, 28 MB database |
| **Retrieval quality** | hybrid search: hit@1 80%, hit@5 97%, MRR@10 0.88 (30-question eval) |
| **Search latency** | p50 38 ms, p95 70 ms (warm, sequential) |
| **Full ingest** | 688 s from GitHub; unchanged re-ingest 12 s |
| **Runtime** | Python, MCP SDK 2.x, PostgreSQL 16 + pgvector, local CPU embeddings, no API keys |

---

## 1. System Overview & High-Level Architecture

### 1.1 Components

```
 Claude Code ──stdio──► mcp-server (Python, one process per Claude session)
                          │  tools: ingest · ingest_status · search · list_sources · delete_source
                          │
                          ├─ Loader      text | file | directory | URL | GitHub repo URL
                          ├─ Parser      RST (primary) + Markdown + HTML → section tree
                          ├─ Chunker     structure-aware, token-based, small-section packing
                          ├─ Embedder    BAAI/bge-small-en-v1.5 (local, CPU, 384-dim)
                          └─ Store       PostgreSQL 16 + pgvector
                                           sources ──1:N──► document_chunks
                                           HNSW (vector) + GIN (full-text)
```

### 1.2 Pipeline flow

**Ingest**

```
load ─► SHA-256 hash ─► unchanged? ──yes──► skip (no embedding)
                            │ no
                            ▼
        parse sections ─► chunk ─► embed (batches of 16) ─► replace chunks in one transaction
```

**Search**

```
query ─► embed (bge query prefix) ─┬─► vector top-30  (HNSW, cosine)        ─┐
                                   └─► keyword top-30 (GIN, IDF-weighted)   ─┴─► RRF (k=60) ─► top-k
                                                                                               │
              Claude answers and cites ◄── Markdown: page > section, source URL, scores, text ◄┘
```

---

## 2. Knowledge Base Analysis & Constraints

The design was driven by an inspection of the corpus before any code was written.

| Finding | Design consequence |
|---|---|
| 817 `.rst` files vs. 28 `.md` (mostly GitHub templates and Flink snippets) | RST parsing is the primary path, not an add-on |
| ~3.2 MB of text ≈ 1M tokens | Full ingest fits in minutes on CPU; a single Postgres instance is sufficient |
| Configuration parameters live in `includes/config-*.rst` and are pulled in via `.. include::` (e.g. `advanced-params.rst` is a single line) | Includes must be resolved inline, or answers to the most-tested questions cite the wrong page |
| Each configuration parameter is a 3–4 line section; some heading underlines are shorter than their titles | Small-section packing and a lenient heading detector are required |
| Frequent directives: `code`/`code-block` (~1,100), `note`, `list-table`, `grid`, `dropdown`, `image`, `tableofcontents` | Keep code and tables (answers live there); drop pure markup noise |
| Repository archived (last commit January 2024) | Treated as a static corpus; re-ingest remains idempotent for changing content |

---

## 3. Component Design & Trade-off Matrix

Each table lists the decision, its rationale, and what it costs.

### 3.1 Ingestion

| Decision | Rationale | Trade-off |
|---|---|---|
| `ingest` takes exactly one of `text` / `path` / `url`. A URL may be a web page, a raw `.md`/`.rst`/`.txt` file, a GitHub repo (shallow `git clone`), a repo subfolder (`/tree/main/docs/...`) or a single GitHub file (`/blob/...`) | The whole repository is one call: `ingest(url="https://github.com/aiven/devportal")` | Requires `git` in the image |
| A directory or repo expands into **one source per document** | `list_sources`, `delete_source`, idempotency and citations all stay per-page | More rows in `sources` (negligible at ~800) |
| **Directory/repo ingests run as background jobs**: `ingest` returns a job ID immediately; `ingest_status` reports progress and ETA. One worker thread; each document commits separately, so content becomes searchable progressively | A full-repo ingest takes ~11.5 min, far too long to block a tool call | Jobs live in memory and are lost if the server exits; re-running resumes cheaply via content hashes |
| Synchronous **CLI** entry point: `docker compose run --rm -T mcp-server ingest <path\|url>` | Bulk loading does not depend on a Claude session staying open | Two entry points (same pipeline underneath) |
| `.. include::` resolved **inline into the parent page**; a file included elsewhere is not a standalone source (detected by scanning includes, not by folder name) | Parameters are cited under their real page; no duplicate chunks; works for any Sphinx repo | Content included by several pages is stored once per including page |
| Repo file `source_uri` = **GitHub blob URL** (e.g. `https://github.com/aiven/devportal/blob/main/docs/...rst`); repo-relative path kept in `metadata.path` | Unique across repositories and always a valid, clickable citation | Long URIs; filtering by substring (`products/kafka`) |
| Local files keyed by absolute container path (`/data/...`); raw text by `text:<name>` or `text:<sha256 prefix>` | Stable identity: re-ingesting the same input replaces it | — |
| **HTML → Markdown**: BeautifulSoup strips nav/header/footer/scripts; `markdownify` keeps h1–h6, lists, tables, code; `<title>` becomes the source title | The structure-aware chunker works unchanged (breadcrumbs from HTML headings) | Two small dependencies; JavaScript-rendered pages are not executed |
| **Re-ingesting a directory/repo synchronizes it**: sources under its URI prefix that no longer exist are deleted, **only after a complete walk with zero failures** | Upstream deletions propagate (verified on real data: 3 stale `.txt` sources removed) | A run with any failure leaves vanished sources in place (reported as `sync_skipped`) |
| Directory/repo scans ingest only `.md`, `.markdown`, `.mdx`, `.rst`; `.git`, `.github`, `node_modules`, `_build` are skipped. An explicit single file may also be `.txt` | Scope is the Markdown and RST content; `requirements.txt`/`robots.txt` were noise | — |
| Limits: 5 MB per document, 30 s HTTP timeout, 5 min clone timeout, `GIT_TERMINAL_PROMPT=0`, `http(s)` URLs only | Bad input fails fast with a clear message and never hangs | Very large single files are rejected |
| Embedding batch size **16** (`INGEST_BATCH_SIZE`) | 32/64 measured no faster on CPU; small batches bound how long a search waits behind an ingest (~3.5 s) | — |
| One failing document never aborts a collection; the first 20 errors are reported per job | An 800-file ingest must not die on one bad file | — |

### 3.2 Parsing (RST-first)

| Decision | Rationale | Trade-off |
|---|---|---|
| Lightweight custom RST parser instead of `docutils` rendering | Sphinx-specific directives (`grid`, `dropdown`, `mermaid`, `:doc:`) make docutils fail or require Sphinx itself; only headings and clean text are needed | Not a full RST implementation; edge cases handled heuristically |
| Heading = text line + underline of one repeated punctuation character, length ≥ 3, **no spaces**; overline variant supported; level = order of first appearance of each style | Matches RST's level semantics; "no spaces" keeps simple-table borders (`====  ====`) from parsing as headings; lenient length tolerates short underlines | Rare false positives on unusual text |
| Markdown: ATX headings (`#`…`######`); `####` and deeper fold into the parent breadcrumb depth | Covers the repo's `.md` files and arbitrary user uploads | Setext-style headings unsupported (rare) |
| `code`/`code-block` kept verbatim as fenced code. Dropped: `image`, `figure`, `toctree`, `tableofcontents`, `raw`, `mermaid`, `literalinclude`. Roles stripped: `` :doc:`text <link>` `` → `text`; ` ``x`` ` → `` `x` `` | Commands and parameter values live in code and tables; markup noise hurts both embeddings and keyword matching | Images, diagrams and `literalinclude`d files are not searchable |
| **Tables: light cleanup** — border lines (`+-----+`) and `:widths:`-style options removed; cell text kept as written | Borders waste tokens; cell text holds the defaults (e.g. `tcp_keepalives_idle` = 180) | Rows are not rewritten as standalone "Header: value" facts |
| Admonitions/containers (`note`, `warning`, `dropdown`, `grid`, `list-table`, …) keep their content with a short label (`Note: …`) | Limits and caveats often live inside notes | — |
| Substitutions (`.. \|name\| replace:: …`) resolved; comments, labels (`.. _x:`) and `:orphan:` dropped | `\|mysql_max_connections\|` would otherwise be an opaque token | Footnotes (`.. [1]`) dropped with comments (rare in this corpus) |
| Page title = first top-level heading, placed at the front of every breadcrumb | Sections from different products look alike ("Prerequisites"); the title disambiguates | — |

### 3.3 Chunking

| Decision | Rationale | Trade-off |
|---|---|---|
| **Structure-aware, token-based** chunking with the embedding model's own tokenizer: **target 350 / max 450 / overlap 50 tokens** (configurable) | Sizes are exact for the model, not character approximations; 350 tokens holds a full how-to step | Slower than character splitting |
| **Sibling subtree packing**: a section whose whole subtree fits in 350 tokens is one unit; consecutive sibling units (plus the parent's intro) are packed up to 350; an oversized subtree is chunked internally and never packed with neighbours | Avoids thousands of ~30-token parameter chunks that embed poorly, without mixing unrelated parents (e.g. `private_access.*` stays together) | One chunk can hold ~8 parameters, so a hit is less pinpointed |
| Breadcrumb = **deepest heading path covering all text in the chunk**; headings with no text produce no chunk | A one-page document gets `[Page]`; a packed subtree gets `[Page > private_access]` | — |
| Long sections split on paragraphs → lines → token windows; **each piece repeats its section heading** | Every piece is readable and self-locating | A few tokens per piece |
| Overlap = trailing whole paragraphs up to 50 tokens, falling back to the **last ~50 tokens of the previous paragraph** (cut at a word boundary) | Real paragraphs often exceed 50 tokens, so whole-paragraph-only overlap was usually zero (caught by a test) | Duplicate text between neighbouring pieces |
| Code fences re-balanced when a code block is split | Every chunk renders as valid Markdown | — |
| **Breadcrumb injected only into `embedding_input`** (e.g. `[Kafka advanced parameters > private_access]`); `content` stays clean | Richer vectors without polluting the text Claude reads | Two text columns per chunk |
| Breadcrumb capped at 48 tokens by eliding middle levels (`[Page > ... > Leaf]`); full path kept in `heading_path` | Deep pages with long titles must still fit the model | Middle levels occasionally missing from embedded text |
| **Hard guarantee: breadcrumb + content ≤ 510 tokens** (512 − `[CLS]`/`[SEP]`); configuration validated at startup | bge-small silently truncates past 512 tokens | — |
| `CHUNKER_VERSION` constant included in the content hash | Changing chunking logic forces re-chunking on the next ingest | Must be bumped manually |

**Measured on the full corpus:** tokens per chunk p5 58 · p50 286 · p95 389 · **max 485** (limit 510).
20 files produce no chunks, all navigation-only pages (title + `tableofcontents`).

### 3.4 Embedding

| Decision | Rationale | Trade-off |
|---|---|---|
| **`BAAI/bge-small-en-v1.5`** (384-dim, 512-token context) over all-MiniLM-L6-v2 | MiniLM truncates at **256 tokens**, which would cut 350–450-token chunks; bge-small retrieves better at the same dimension | ~2× slower than MiniLM on CPU |
| Query prefix `"Represent this sentence for searching relevant passages: "` on queries only | Required by bge for asymmetric retrieval | — |
| Model **downloaded at `docker build`**, then `HF_HUB_OFFLINE=1`; CPU-only PyTorch wheel | No network on first run and no API key (verified with `--network none`); no CUDA bloat | 2.6 GB image; changing the model requires a rebuild |
| Normalized embeddings + cosine distance | Standard for bge; scores are comparable | — |
| PyTorch runtime (not ONNX Runtime / int8) | Lowest-risk path; throughput (~4.4 chunks/s) is acceptable with background jobs | ONNX int8 would likely be 2–3× faster and ~700 MB smaller |
| Model loads in a **background thread**; tools wait for it rather than fail | Importing torch + sentence-transformers takes ~10 s; blocking the MCP handshake risks a client startup timeout | The first search of a session takes ~14 s |
| Model calls are serialized; **searches take priority over ingest batches** (ingest yields to waiting searches between batches) | A lock around a whole document made searches wait up to **62 s** during ingest; a per-batch lock still starved them (CPython locks are unfair). With priority: **max 4.4 s**. A regression test fails on the old code | A search during ingest still waits up to one batch (~3 s) |
| Model loaded once per process and reused | No per-request load cost | Memory held for the session (375 MiB) |

### 3.5 Storage, Schema & Consistency

**Schema**

| Decision | Rationale | Trade-off |
|---|---|---|
| PostgreSQL 16 + pgvector in one container on a named volume | One store for vectors, full-text and metadata: nothing to keep in sync | Not horizontally scalable |
| `sources`: `source_uri` (UNIQUE), `source_type` CHECK in (`file`, `url`, `text`), `title`, `content_hash`, `chunk_count`, `total_tokens`, `metadata` JSONB, timestamps | Tracks what is ingested; powers `list_sources` and idempotency | — |
| `document_chunks`: `content`, `embedding_input`, `token_count`, `heading_path TEXT[]`, `metadata` JSONB, `embedding vector(384)` | Everything search needs in one row | — |
| FK `ON DELETE CASCADE`, `UNIQUE(source_id, chunk_index)`, index on `source_id` | Database-enforced integrity; cheap deletes and re-ingest | — |
| **HNSW index** (cosine) on `embedding` | Fast approximate search with no training step (unlike IVFFlat) and high recall at this size | Slower inserts, more memory than IVFFlat |
| **Generated `tsvector` column** over `embedding_input` (`simple` config) + GIN index | Keyword search includes titles and breadcrumbs; no stemming, so `max_connections` matches exactly | No stemming for natural-language words (the vector side covers paraphrases) |
| Schema (incl. `CREATE EXTENSION vector`) created **idempotently at server startup** from `src/rag_mcp/schema.sql`, guarded by a Postgres advisory lock | `docker-entrypoint-initdb.d` only runs on a fresh volume; startup creation also works on existing volumes, and the lock prevents races between concurrent servers | No migration tool (Alembic in production) |
| Startup **verifies the vector column dimension** against the configured model | Switching models on an existing volume fails immediately with a clear message, not mid-ingest | — |

**Idempotency & consistency**

| Decision | Rationale | Trade-off |
|---|---|---|
| Content hash = SHA-256 of **content + chunker version + model name** | Identical content is skipped; changing chunking or the model correctly triggers re-embedding | — |
| Unchanged (`source_uri`, hash) → return early, no embedding | Re-ingesting the full repository takes 12 s | — |
| Changed → delete old chunks + insert new **in one transaction** | Readers never see a half-updated source | Brief lock on that source's rows |
| Upsert on `UNIQUE(source_uri)` plus a per-source advisory lock | Concurrent ingests of the same URI cannot create duplicates | — |
| Parse/chunk/embed run **before** the transaction; the transaction only does lock → upsert → delete → insert | No database locks held during minutes of CPU work | Concurrent ingests of one URI both embed (last write wins) |
| Documents with no text are recorded with `chunk_count = 0` | Visible in `list_sources` and skipped by hash next time | — |

### 3.6 Hybrid Search

| Decision | Rationale | Trade-off |
|---|---|---|
| **Vector top-30 + keyword top-30, fused with Reciprocal Rank Fusion (k=60)** | Questions target exact tokens (parameter names, limits) where vectors are fuzzy; RRF needs no score calibration | Two retrievers per query |
| Keyword query: **stopwords dropped, terms OR-matched**; identifiers Postgres would split (`max_connections`) matched as an **exact phrase**; dotted names kept as one token | `websearch_to_tsquery` ANDs every word, so natural-language questions almost never matched | No stemming |
| **Keyword ranking = BM25-style IDF**: score = Σ log(1 + (N − df + 0.5)/(df + 0.5)) over matched terms, with df per term computed via the GIN index at query time | `ts_rank` has no IDF: pages repeating "Aiven PostgreSQL" outranked the one chunk containing `tcp_keepalives_idle` (correct page moved **5th → 1st**) | No term-frequency saturation or length normalization; per-query df counting is the main scale hotspot (§4.5) |
| **Plain RRF**, chosen by evaluation: an identifier boost (×2) produced identical results on all 30 questions | Simpler, with no measured benefit lost | RRF's known weakness remains: a chunk ranked #1 by only one retriever can lose to chunks decent on both (question r06: keyword #1 → hybrid #9) |
| Each retriever takes its top-N in a subquery; ranking happens outside | A window function around `ORDER BY … LIMIT` forces scoring every row and **bypasses HNSW** | — |
| `source_filter` = case-insensitive substring of `source_uri`; filtered queries use `hnsw.iterative_scan = relaxed_order` (pgvector 0.8) | Filtered HNSW scans otherwise run out of candidates before the LIMIT | — |
| **Results as Markdown**: `[n] Page > Section`, `source:` URL, score line (vector rank, keyword rank), chunk text | Natural for LLMs to read; citations are explicit; the score line makes retrieval debuggable | Not machine-structured |
| **Default 5 results**, `top_k` 1–20 | ~1,500 tokens per search; Claude can search again | Broad questions may need a second call |
| No reranker model | Avoids extra latency and a second model; hybrid captures most of the gain | Precision left on the table |

### 3.7 MCP Server & Runtime

| Decision | Rationale | Trade-off |
|---|---|---|
| Python + official `mcp` SDK **2.x** (`MCPServer`), stdio transport | Required transport; best embedding/pgvector ecosystem; v2 API verified against the installed package | — |
| Tools `ingest`, `ingest_status`, `search`, `list_sources`, `delete_source`, with annotations (read-only / destructive / idempotent / open-world) | The required three, plus status for background jobs and delete for changing content; annotations let clients reason about safety | — |
| Server `instructions`: search before answering, cite the source URL, one search per sub-question, say so when nothing is found | Steers Claude toward grounded, cited answers | — |
| Tool functions are synchronous; the SDK runs them in worker threads | A slow search or ingest never blocks the protocol loop | — |
| Invalid input → `ToolError` with an actionable message (e.g. "mount files under ./data and use /data/...") | Claude sees `isError` and can self-correct | — |
| **`list_sources` returns totals + running jobs + filtered, paged rows** (default 50, max 200) | The full corpus would be ~25k tokens in one response | `filter`/`offset` needed to see everything |
| `delete_source` by exact URI only | Prevents accidental bulk deletes; repo re-ingest handles bulk removal via sync | No delete-by-prefix |
| **All logs to stderr**; stdout reserved for JSON-RPC; Hugging Face progress bars disabled | Any stdout write corrupts the stdio transport (verified: raw stdout is pure JSON-RPC) | — |
| `docker compose up -d` starts Postgres only; the server is a compose **profile** service launched by the MCP config as `docker compose -f <path> run --rm -T mcp-server` | With stdio, Claude **spawns** the server; a long-running compose service does not fit | Container + Python start-up on each Claude session |
| `mcp-server` waits on the `db` healthcheck (`pg_isready`) | No connection-failure cascades at startup | — |
| Database bound to `127.0.0.1:5433` only | No clash with a local Postgres on 5432; unreachable from the network | — |
| Separate `rag_test` database for the test suite | Running tests never wipes ingested data | — |

---

## 4. Benchmark & Evaluation Results

Environment: laptop, Docker VM with 8 vCPU / 3.7 GiB, full devportal corpus (814 sources, 2,907 chunks).
Reproduce with `scripts/eval_retrieval.py` and `scripts/benchmark.py`.

### 4.1 Retrieval quality

30 questions ([eval/RESULTS.md](../eval/RESULTS.md)): 20 from randomly sampled pages (`random.Random(42)`),
10 targeted at configuration parameters, limits and how-to steps. A question is answered at rank *r*
if the *r*-th chunk comes from a page that contains the answer.

| Configuration | hit@1 | hit@5 | MRR@10 |
|---|---|---|---|
| Vector only | 70% | 90% | 0.79 |
| Keyword only (IDF) | 73% | 93% | 0.83 |
| **Hybrid (RRF)** | **80%** | **97%** | **0.88** |
| Hybrid + identifier boost ×2 | 80% | 97% | 0.88 |

Vector and keyword retrieval fail on *different* questions; hybrid recovers most of both.
Caveats: n = 30 (one question = 3.3%); questions written by the system author (mitigated by random
page sampling); the metric measures whether the right page is retrieved, not answer correctness.

### 4.2 Ingest performance

| Metric | Result |
|---|---|
| Full devportal ingest from GitHub (CLI) | **688 s (11.5 min)**, 0 failures (clone + chunk + embed + write) |
| Re-ingest, nothing changed | **12–13 s** (clone + hash only, 0 chunks written) |
| Embedding throughput (CPU, batch 16–64) | **~4.4 chunks/s**; 4 vs. 8 threads makes no difference |
| Chunking only, whole repo | 46 s (dominated by repeated token counting) |
| Single document, synchronous tool path | ~5 chunks/s: 10 KB 1.3–2.6 s · 100 KB 14–19 s · **1 MB 132–148 s** (743 chunks) |
| Database size after full ingest | 28 MB (incl. HNSW + GIN indexes) |
| Largest single source | `cassandra-metrics-prometheus.rst`: 181 chunks (6% of the corpus) |

### 4.3 Search latency

| Metric | Result |
|---|---|
| Warm search, sequential | p50 **38 ms**, p95 70 ms (embedding 29 ms, SQL 11 ms) |
| Over real `docker compose run -T` stdio | handshake **7.9 s** (container start); first search **14.4 s** (waits for model load); then **~0.1 s** per search |
| Search during a running ingest | p95 **4.0 s**, max **4.4 s** (before search priority: p95 37 s, max 62 s) |

| Startup component | Time |
|---|---|
| Python import of torch + sentence-transformers | ~10 s |
| Model load (bge-small, from baked cache) | 0.5 s |

### 4.4 Concurrency

| Concurrent clients (one server process) | p50 latency |
|---|---|
| 1 | 51 ms |
| 4 | 245 ms |
| 16 | **1.1 s** |

Throughput ceiling: **~14–19 requests/s** per process; latency grows linearly with concurrent
clients because model calls are serialized. Memory per server process (model loaded): **375 MiB**.

### 4.5 Scale limits

Real chunks replicated with perturbed vectors in a separate database:

| Chunks | Vector top-30 p50 / p95 | Keyword top-30 p50 / p95 | Full search p50 / p95 | HNSW build | DB size |
|---|---|---|---|---|---|
| 2,907 (1×) | 0.9 / 3.0 ms | 5.8 / 10.8 ms | **8 / 13 ms** | 0.9 s | 28 MB |
| 29,070 (10×) | 2.2 / 7.8 ms | 102 / 230 ms | **130 / 415 ms** | 9.2 s | 201 MB |
| 145,350 (50×) | 2.1 / 6.7 ms | 512 / 830 ms | **507 / 809 ms** | 101 s | 963 MB |

HNSW stays at ~2 ms; the keyword side grows linearly with corpus size because IDF is computed per
query (worst case here: replicated chunks share vocabulary exactly).

### 4.6 Verification

78 automated tests, run against a dedicated `rag_test` database.

| Area | Coverage |
|---|---|
| Schema | Idempotent creation, dimension mismatch fails loudly, cascade delete, duplicate chunk/URI rejected, source-type CHECK, full-text matches identifiers exactly |
| Parser & chunker (28 tests) | RST heading levels, table borders not parsed as headings, short underlines, include resolution, small-section packing, token limit never exceeded (incl. breadcrumb), overlap only within a split section |
| Ingest | Loaders (includes, HTML, GitHub URLs, local HTTP server), unchanged content not re-embedded, atomic replace, sync and no-sync-after-failure, background jobs, real-model embedder, search priority during ingest |
| Search | Stopword/identifier term extraction, IDF regression (rare identifier ranks keyword #1), parameter question finds the parameter page, semantic match without shared keywords, source filter, citation format, empty-index message |
| Integration | Real server spawned over stdio with the MCP client: tool list, ingest/unchanged, cited search, `list_sources`, input errors as `isError`, delete, background directory job via `ingest_status` |

---

## 5. Key Engineering Trade-offs & Limitations

### 5.1 Principal trade-offs

| Choice | Over | Gained | Gave up |
|---|---|---|---|
| bge-small (384-dim) | bge-base / nomic | CPU speed, smaller image, 512-token context | Some semantic quality; hybrid search compensates on exact-term questions |
| Hybrid (vector + IDF keyword + RRF) | Vector only | hit@1 70% → 80%, hit@5 90% → 97% | An extra retriever per query and more code |
| Custom RST parser | docutils / Sphinx | Robust on Sphinx-only directives, no heavy dependencies | Full RST fidelity |
| Background ingest jobs | Synchronous ingest only | Whole-repo ingest from Claude without blocking; progressive availability | Job state is in memory and lost on exit |
| HNSW | IVFFlat / exact scan | ~2 ms vector search up to 145k chunks, no training step | Insert speed and memory |
| Single Postgres for vectors + full-text + metadata | Dedicated vector DB + search engine | One consistent store, transactional re-ingest | Horizontal scalability |

### 5.2 Breaking points, in order

1. **Embedding throughput.** The CPU-bound model limits ingest to ~5 chunks/s and search to ~15 req/s
   per process. A 1 MB document blocks the synchronous `ingest` call for ~2.5 min (the 5 MB limit ≈ 12 min).
2. **Keyword search at data growth.** Per-query IDF counting grows linearly (6 → 102 → 512 ms at
   1× / 10× / 50×). Fix: term statistics maintained at ingest time, or a dedicated BM25 index.
3. **Memory per session.** 375 MiB per server process × concurrent Claude sessions.
4. **HNSW build time.** 101 s at 145k chunks with default `maintenance_work_mem`; affects full rebuilds,
   not incremental inserts.

### 5.3 Limitations & assumptions

- Scope is the `.rst` and `.md` content; `includes/` is inlined into parent pages; `.github/` is excluded.
- English-only content; full-text search uses the `simple` configuration (no stemming).
- Single user per server process; no authentication.
- Background ingest jobs are held in memory and do not survive a server restart.
- Images, diagrams and `literalinclude`d files are not searchable; JavaScript-rendered web pages are not executed.
- Public docs URLs are derived from file paths and may not resolve for every page (the repository is archived).
- Schema changes have no migration tool.

### 5.4 Next steps

| Improvement | Expected effect |
|---|---|
| ONNX Runtime with int8 quantization | ~2–3× faster embedding, ~700 MB smaller image |
| Precomputed term statistics or a BM25 index | Keyword search flat with corpus size |
| Cross-encoder reranker on the fused top-30 | Higher precision at top-1 |
| Persistent job queue | Ingest jobs survive restarts |
| Faster token counting in the chunker | Chunking time (46 s) reduced |
