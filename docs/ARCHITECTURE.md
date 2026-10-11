# RAG MCP Server — Architecture & Decision Log

An MCP server (stdio) that lets Claude ingest documents and answer questions over them using
retrieval. Target knowledge base: the full [aiven/devportal](https://github.com/aiven/devportal) repo.

---

## 1. System overview

```
 Claude Code ──stdio──► mcp-server (Python, one process)
                          │  tools: ingest · search · list_sources · delete_source
                          │
                          ├─ Loader      text | file | directory | URL | GitHub repo URL
                          ├─ Parser      RST (primary) + Markdown → section tree
                          ├─ Chunker     structure-aware, token-based, small-section merging
                          ├─ Embedder    BAAI/bge-small-en-v1.5 (local, CPU, 384-dim)
                          └─ Store       PostgreSQL 16 + pgvector
                                           sources ──1:N──► document_chunks
                                           HNSW (vector) + GIN (full-text)
```

**Main flow — ingest:** load → hash → (skip if unchanged) → parse sections → chunk →
embed in batches → replace chunks atomically.

**Main flow — search:** embed query → vector top-30 + keyword top-30 → Reciprocal Rank
Fusion → top-k chunks with source, heading path and docs URL → Claude answers and cites.

---

## 2. Findings about the knowledge base (drove the design)

| Finding | Impact |
|---|---|
| 817 `.rst` files vs 28 `.md` (mostly GitHub templates / Flink snippets) | RST parsing is the main path, not an extra |
| ~3.2 MB text ≈ ~1M tokens → est. 4–6k chunks | Full ingest fits in minutes on CPU; single Postgres is fine |
| Config parameters live in `includes/config-*.rst`, pulled in via `.. include::` (e.g. `advanced-params.rst` is one line) | Includes must be resolved, or the most-tested answers cite the wrong page |
| Each config parameter is a tiny 3–4 line section; some heading underlines are shorter than the title | Need small-section merging and a lenient heading detector |
| Common directives: `code`/`code-block` (~1,100), `note`, `list-table`, `grid`, `dropdown`, `image`, `tableofcontents` | Keep code and tables (answers live there), drop pure noise |
| Repo is archived (last commit Jan 2024) | Treat as a static corpus; re-ingest is still idempotent |

---

## 3. Decisions by component

### 3.1 Ingestion & loading

| Decision | Why | What we give up |
|---|---|---|
| `ingest` takes exactly one of `text` / `path` / `url`; a URL may be a web page, a raw `.md`/`.rst`/`.txt` file, a GitHub repo (shallow `git clone`), a repo subfolder (`/tree/main/docs/...`) or a single GitHub file (`/blob/...`) | Lets Claude ingest the whole repo with one call: `ingest(url="https://github.com/aiven/devportal")` | Needs `git` in the image |
| A directory/repo expands into **one `file` source per document** | `list_sources`/`delete_source`/idempotency stay per-page; citations are per-page | More rows in `sources` (fine at ~800) |
| **Big inputs (directory/repo) run as a background job** (chosen with Amit): `ingest` returns a job id at once, an `ingest_status` tool reports progress + ETA; one worker thread; each document is committed separately, so docs become searchable progressively | Measured: whole repo ≈ 12 min (embedding ~4.4 chunks/s) — too long to block a tool call | Jobs live in memory: lost if the server exits (re-running resumes cheaply via the hash) |
| Also a **CLI command** (`docker compose run --rm -T mcp-server ingest <path\|url>`), synchronous | Bulk load without depending on a Claude session staying open | Two entry points (same pipeline underneath) |
| Resolve `.. include::` **inline into the parent page**; a file that another file includes is **not a standalone source** (detected by scanning includes, not by folder name) | Parameters are cited under the real page (`kafka/reference/advanced-params.rst`); no duplicate chunks; works for any Sphinx repo | Content included by several pages is stored once per including page |
| **Repo file `source_uri` = GitHub blob URL** (chosen with Amit), e.g. `https://github.com/aiven/devportal/blob/main/docs/...rst`; repo-relative path kept in `metadata.path` | Unique across repos and always a valid, clickable citation | Long URIs; filtering by substring (`products/kafka`) |
| Local files: absolute path in the container (`/data/...`); text: `text:<name>` (or `text:<sha256 prefix>` without a name) | Stable identity, so re-ingesting the same input replaces it | — |
| **HTML pages → Markdown** (chosen with Amit): BeautifulSoup drops nav/header/footer/scripts, `markdownify` keeps h1–h6, lists, tables, code; page `<title>` becomes the source title | The structure-aware chunker keeps working (breadcrumbs from HTML headings) | Two small deps (bs4, markdownify); JS-rendered pages are not executed |
| **Re-ingesting a directory/repo syncs it**: sources under its URI prefix that no longer exist are deleted — **only after a complete walk with zero failures** | "Content changes frequently" story: deletions upstream propagate | A run with any failure leaves vanished sources in place (reported as `sync_skipped`) |
| Limits: 5 MB per document, 30 s HTTP timeout, 5 min clone timeout, `GIT_TERMINAL_PROMPT=0`, only `http(s)` URLs | A bad input must fail fast with a clear message, never hang | Very large single files are rejected |
| Embedding batch size **16** (`INGEST_BATCH_SIZE`) | Measured: 32/64 are no faster on CPU; small batches mean a search waits at most one batch (~3.5 s) behind a running ingest | — |

### 3.2 Parsing (RST-first)

| Decision | Why | What we give up |
|---|---|---|
| Own lightweight RST heading parser instead of full `docutils` rendering | Sphinx-specific directives (`grid`, `dropdown`, `mermaid`, `:doc:`) make docutils error out or need Sphinx itself; we only need headings + clean text | Not a full RST implementation; edge cases handled heuristically |
| Heading = text line + underline of one repeated punctuation char, length ≥ 3, **no spaces**; overline variant supported; level = order of first appearance of each style | That's how RST defines levels; "no spaces" prevents simple-table borders (`====  ====`) being read as headings; lenient length tolerates the repo's sloppy underlines | Rare false positives on unusual text |
| Markdown: ATX headers (`#`…`######`); `####`+ fold into parent breadcrumb depth | Covers the `.md` files and arbitrary user uploads | No Setext-style MD headings (rare) |
| Keep `code`/`code-block` as fenced code (verbatim, no markup cleanup); drop `image`, `figure`, `toctree`, `tableofcontents`, `raw`, `mermaid`, `literalinclude`; strip roles like `` :doc:`text <link>` `` → `text`; ` ``x`` ` → `` `x` `` | Commands and parameter values live in code/tables; markup noise hurts both embeddings and keyword matching | Images/diagrams and `literalinclude`d code files are not searchable |
| **Tables: light cleanup** (chosen with Amit) — drop grid/simple-table border lines and `:widths:`-style options, keep cell text as written | Borders like `+-----+` burn tokens and add noise; cell text is where defaults live (`tcp_keepalives_idle` = 180) | Rows are not rewritten as standalone facts (alternative: "Header: value" rows — more code, more bug risk) |
| Admonitions/containers (`note`, `warning`, `dropdown`, `grid`, `list-table`, ...) keep their content, with a short label (`Note: ...`) | Limits and caveats often live inside notes | — |
| Substitutions (`.. \|name\| replace:: ...`) resolved; comments, labels (`.. _x:`), `:orphan:` dropped | `\|mysql_max_connections\|` would otherwise be an opaque token | Footnotes (`.. [1]`) are dropped with comments (rare in this corpus) |
| Page title = first top-level heading (front of every breadcrumb) | Chunks from different products look alike ("Prerequisites") — the title disambiguates | — |

### 3.3 Chunking

| Decision | Why | What we give up |
|---|---|---|
| **Structure-aware, token-based** chunking using the embedding model's own tokenizer; **target 350 / max 450 / overlap 50 tokens** (chosen with Amit; configurable, to be tuned with the eval set in step 5) | Sizes are exact for the model, not approximations from characters; 350 is enough context for how-to steps | Slightly slower than char splitting |
| **Pack whole subtrees of siblings** (chosen with Amit): a section whose whole subtree fits in 350 tokens is one unit; consecutive sibling units (plus the parent's intro) are packed up to 350; a big subtree is chunked internally and is never packed with its neighbours | Avoids thousands of ~30-token chunks (config params) that embed poorly, while never mixing text from unrelated parents (e.g. `private_access.*` stays together) | A chunk can hold ~8 parameters, so a hit is less pinpointed than one-param-per-chunk |
| Breadcrumb = **deepest path that covers all text in the chunk**; a heading with no text under it produces no chunk | A one-page document gets `[Page]`, a packed `private_access` subtree gets `[Page > private_access]` | — |
| Long sections split on paragraphs → lines → token windows; **each piece repeats the section heading** | Readable pieces; Claude sees which section a piece belongs to | A few tokens per piece |
| Overlap = trailing whole paragraphs up to 50 tokens, or **the last ~50 tokens of the previous paragraph** (cut at a word boundary) when no whole paragraph fits | Found by a test: real paragraphs are often >50 tokens, so whole-paragraph-only overlap was usually zero | Duplicate text between neighbouring pieces |
| Code fences re-balanced when a code block is cut between pieces | Every chunk renders as valid Markdown | — |
| **Breadcrumb injected only into `embedding_input`** (e.g. `[Kafka advanced parameters > private_access]`), `content` stays clean | Richer vectors without polluting what Claude reads | Two text columns per chunk (storage) |
| Breadcrumb capped at 48 tokens by dropping middle levels (`[Page > ... > Leaf]`); full path still stored in `heading_path` | Deep pages with long titles must still fit the model | Middle levels missing from the embedded text in rare cases |
| Hard guarantee: breadcrumb + content ≤ 510 tokens (512 − [CLS]/[SEP]); config validated at startup | bge-small truncates at 512 tokens — anything beyond is silently not embedded | — |
| `CHUNKER_VERSION` constant, part of the content hash | Changing chunking logic forces re-chunking on the next ingest | Must remember to bump it |

### 3.4 Embeddings

| Decision | Why | What we give up |
|---|---|---|
| **`BAAI/bge-small-en-v1.5`** (384-dim, 512-token context) instead of all-MiniLM-L6-v2 | MiniLM truncates at **256 tokens** → our 350–450-token chunks would be cut; bge-small has better retrieval quality and the same dimension | ~2× slower than MiniLM on CPU |
| Query prefix `"Represent this sentence for searching relevant passages: "` on queries only | Required by bge for asymmetric retrieval | — |
| Model **downloaded at `docker build`** time, then `HF_HUB_OFFLINE=1`; CPU-only PyTorch wheel | No network on first run, no API key (verified: loads with `--network none`); image stays lean without CUDA | Bigger image than a no-model image; rebuild to change model |
| Normalized embeddings + cosine distance | Standard for bge; makes scores comparable | — |
| **Keep PyTorch for now** (chosen with Amit) instead of ONNX Runtime / int8 | No new risk mid-project; measured throughput (4.4 chunks/s) is acceptable with background jobs | ONNX int8 would likely be 2–3× faster and ~700 MB smaller — top "next level" item |
| Model loads in a **background thread** | Handshake isn't blocked by the ~10 s import | First search waits for the load |
| Model calls run one at a time; **searches have priority over ingest batches** (ingest embeds batch by batch and yields to waiting searches) | Found by the load benchmark: a plain lock around the whole document made searches wait up to **62 s** during an ingest; a per-batch lock still starved them (CPython locks are unfair: p95 31 s). With priority: **max 4.4 s** (one batch). Regression test fails on the old code | A search during an ingest still waits ~3 s for the running batch (smaller batches = shorter wait, slower ingest) |
| (Optional) hosted provider via env var — **not planned for v1** | Not required; keeps dependencies minimal | — |

### 3.5 Storage & schema

| Decision | Why | What we give up |
|---|---|---|
| PostgreSQL 16 + pgvector in one container, named volume | Required; one store for vectors, text search, and metadata = no sync problems | Not horizontally scalable (fine for scope) |
| `sources` table: `source_uri` (UNIQUE), `source_type` CHECK in (`file`,`url`,`text`), `title`, `content_hash`, `chunk_count`, `total_tokens`, `metadata` JSONB, timestamps | Tracks what's ingested; powers `list_sources` and idempotency | — |
| `document_chunks`: `content`, `embedding_input`, `token_count`, `heading_path TEXT[]`, `metadata` JSONB, `embedding vector(384)` | Everything search needs in one row | — |
| FK `ON DELETE CASCADE`, `UNIQUE(source_id, chunk_index)`, index on `source_id` | DB-enforced integrity; cheap deletes and re-ingest | — |
| **HNSW index** (cosine) on `embedding` | Fast approximate search, no training step (unlike IVFFlat), good recall at this size | Slower inserts, more memory than IVFFlat |
| **Generated `tsvector` column** over `embedding_input` (`simple` config) + GIN index | Enables keyword search incl. page titles/breadcrumbs; `simple` = no stemming, so `max_connections` matches exactly | No stemming for natural-language words (vector side covers that) |
| Schema (incl. `CREATE EXTENSION vector`) created **idempotently at server startup** from `src/rag_mcp/schema.sql`; no `init.sql` | `docker-entrypoint-initdb.d` runs only on a fresh volume — startup creation works on existing volumes too; one source of truth | No real migration tool (would add Alembic in production) |
| Schema creation guarded by a Postgres **advisory lock** | Two server processes starting at once can race even with `IF NOT EXISTS` | — |
| Startup **checks the vector column dimension** against the configured model | Switching models on an existing volume fails immediately with a clear message, not mid-ingest | — |

### 3.6 Idempotency & consistency

| Decision | Why | What we give up |
|---|---|---|
| SHA-256 of **content + chunker version + model name** | Same content is skipped; changing chunk settings or the model correctly triggers re-embedding | — |
| Unchanged (`source_uri`, hash) → return early, no embedding | Re-ingesting the whole repo is near-free | — |
| Changed → delete old chunks + insert new in **one transaction** | Readers never see a half-updated source | Brief lock on that source's rows |
| Upsert on `UNIQUE(source_uri)` (+ per-source advisory lock) | Two concurrent ingests of the same URI can't race into duplicates | — |
| Parse/chunk/embed happen **before** the transaction; the transaction only does lock → upsert → delete → insert | No DB locks held during minutes of CPU work | Two concurrent ingests of one URI both do the embedding work (last write wins) |
| A document with no text (navigation page) is still recorded, with `chunk_count = 0` | Shows up in `list_sources` and is skipped by hash next time | — |
| One failing document never aborts a collection; errors (first 20) are reported per job | An 800-file ingest shouldn't die on one bad file | — |

### 3.7 Retrieval (search)

| Decision | Why | What we give up |
|---|---|---|
| **Hybrid search: vector top-30 + keyword top-30, fused with RRF (k=60)** | Test questions target exact tokens (parameter names, limits) where vectors are fuzzy; RRF needs no score calibration | Two retrievers per query (still ~100 ms warm) |
| Keyword query: **drop stopwords, match each term separately (OR)**; identifiers Postgres splits (`max_connections`) matched as an **exact phrase**; dotted names stay one token | Measured: `websearch_to_tsquery` ANDs every word ("how & many & can & …") → natural-language questions would almost never match | No stemming (vector side covers paraphrases) |
| **Keyword ranking = BM25-style IDF** (chosen with Amit): score = Σ log(1 + (N − df + .5)/(df + .5)) over matched terms; df per term via the GIN index, computed per query | Found on real data: `ts_rank` has no IDF, so pages repeating "Aiven PostgreSQL" beat the one chunk with `tcp_keepalives_idle` (correct page came **5th → 1st** after the fix) | No term-frequency saturation / length normalisation (not full BM25); df counting costs O(matches) per term — a scale hotspot (precomputed term stats or a BM25 index at 100×) |
| **Plain RRF kept — decided by the eval** (rule chosen with Amit: decide with data). Identifier boost ×2 gave identical results on all 30 questions | Simpler, no measured benefit | RRF's known weakness remains: a chunk #1 on one side only can lose to chunks decent on both (eval question r06: keyword #1 → hybrid #9) |
| Each retriever takes its top-N in a subquery, ranks outside | A window function around `ORDER BY … LIMIT` forces scoring every row and **bypasses HNSW** | — |
| `source_filter` = case-insensitive substring of `source_uri`; with a filter, `hnsw.iterative_scan = relaxed_order` (pgvector 0.8) | Filtered HNSW scans otherwise run out of candidates before the LIMIT | — |
| **Results as readable Markdown** (chosen with Amit): `[n] Page > Section`, `source:` URL, score line (vector #, keyword #), chunk text | LLMs read it naturally; citations obvious; the score line makes retrieval debuggable | Not machine-structured |
| **Default 5 results** (chosen with Amit), `top_k` 1–20 | ~1,500 tokens per search; Claude can search again | Broad questions may need a second call |
| No reranker model in v1 | Adds latency + another model; hybrid gets most of the gain | Precision left on the table ("next level") |

### 3.8 MCP server & runtime

| Decision | Why | What we give up |
|---|---|---|
| Python + official `mcp` SDK **2.x** (`MCPServer`, formerly FastMCP), stdio transport | Required transport; verified the v2 API in the installed package rather than assuming v1 | — |
| Tools: `ingest`, `ingest_status`, `search`, `list_sources`, `delete_source`, with annotations (read-only / destructive / idempotent / open-world) | Required three + status for background jobs + delete for the "content changes" story; annotations let clients reason about safety | — |
| Server `instructions`: search before answering, cite the source URL, one search per sub-question, say so when nothing is found | Steers Claude toward grounded, cited answers | — |
| Tool functions are synchronous; the SDK runs them in worker threads | A slow search/ingest never blocks the protocol loop | — |
| Bad input → `ToolError` with an actionable message (e.g. "mount files under ./data and use /data/...") | Claude sees `isError` and can correct itself | — |
| **`list_sources` = totals + running jobs + filtered, paged rows** (chosen with Amit; default 50, max 200) | The full repo would be ~25k tokens in one response | Need `filter`/`offset` to see everything |
| `delete_source` by exact URI only | Prevents accidental bulk deletes; repo re-ingest handles bulk removal via sync | No "delete everything under a prefix" tool |
| **All logs to stderr**; stdout reserved for the protocol; HF progress bars disabled | Any stdout write corrupts MCP stdio (verified: raw stdout is pure JSON-RPC) | — |
| `docker compose up -d` starts Postgres; server service under a compose **profile**; MCP config runs `docker compose -f <path> run --rm -T mcp-server` | With stdio, Claude **spawns** the server — a long-running compose service doesn't fit | Container + Python start on each Claude session |
| **Handshake first, model in the background; tools wait for the model** (chosen with Amit: wait, don't fail) | Importing torch takes ~10 s; blocking the handshake risks a client startup timeout | The first search of a session takes ~14 s |
| Directory/repo scans ingest only `.md/.markdown/.mdx/.rst`; an explicit single file may also be `.txt` | The task says "the Markdown and RST content"; `requirements.txt`/`robots.txt` were noise (removed by sync: 3 deleted) | — |
| `mcp-server` waits on `db` healthcheck (`pg_isready`) | Avoids connection-failure cascades on startup | — |
| DB exposed only on `127.0.0.1:5433` | Avoids clashing with a local Postgres on 5432; not reachable from the network | — |
| Separate `rag_test` database for the test suite | Running tests never wipes ingested data | — |
| Model loaded once per process, reused across calls | Avoids per-request load cost | Memory held for the session |

---|---|---|
| Python + official `mcp` SDK, stdio transport | Required transport; Python has the best embedding/pgvector ecosystem | — |
| Tools: `ingest`, `search`, `list_sources`, `delete_source` | Required three + delete (cheap, supports the "content changes" story) | — |
| **All logs to stderr**; stdout reserved for the protocol | Any stdout write corrupts MCP stdio | — |
| `docker compose up -d` starts Postgres; server service under a compose **profile**; MCP config runs `docker compose -f <path> run --rm -T mcp-server` | With stdio, Claude **spawns** the server — a long-running compose service doesn't fit | Container + Python start on each Claude session |
| **Answer the MCP handshake first, load the model in the background** | Measured: importing torch + sentence-transformers takes ~10 s (model load itself 0.5 s); blocking on it risks a startup timeout | First search may wait for the load to finish |
| `mcp-server` waits on `db` healthcheck (`pg_isready`) | Avoids connection-failure cascades on startup | — |
| DB exposed only on `127.0.0.1:5433` | Avoids clashing with a local Postgres on 5432; not reachable from the network | — |
| Separate `rag_test` database for the test suite | Running tests never wipes ingested data | — |
| Model loaded once per process, reused across calls | Avoids per-request load cost | Memory held for the session |

---

## 4. Testing strategy (what we test and why)

- **Schema (done):** idempotent creation, dimension mismatch fails loudly, cascade delete,
  duplicate chunk/URI rejected, source type CHECK, full-text matches identifiers exactly.
- **Chunker/parser (done, 28 tests):** RST heading levels, table borders not treated as headings,
  sloppy underlines, include resolution, small-section merging, token limits never exceeded
  (incl. breadcrumb), overlap only within a split section.
- **Ingest (done):** loaders (includes, HTML, GitHub URLs, local HTTP server), idempotency, atomic replace, sync + no-sync-after-failure, background jobs, real-model embedder. Same content → no re-embed; changed content → chunks replaced atomically;
  delete cascades.
- **Search (done):** stopword/identifier term extraction, IDF regression (rare identifier is keyword #1), parameter question finds the parameter page, semantic question without shared keywords, source filter, citation format, empty index message.
- **Integration (done):** the real server spawned over stdio with the MCP client: tool list, ingest/unchanged, search with citation, list_sources, input errors as `isError`, delete, background directory job via `ingest_status`.
- **Retrieval eval:** ~25 questions with known source pages → recall@5 / MRR (also feeds the deck).
- **Load benchmark:** ingest throughput, p50/p95 search latency, concurrent queries — real numbers
  for the "Load" and "Where it breaks" slides.

---

## 5. Candidate tradeoffs for the deck (slide 6)

1. **bge-small vs. larger model (bge-base / nomic)** — gave up some quality for CPU speed and a
   smaller image; hybrid search compensates on exact-term questions.
2. **Hybrid (vector + FTS + RRF) vs. vector-only** — gave up simplicity and one extra query for big
   gains on parameter/limit questions.
3. **Own RST parser vs. docutils/Sphinx** — gave up full RST fidelity for robustness on
   Sphinx-specific directives and zero extra heavy deps.
4. **Synchronous ingest vs. background job queue** — gave up non-blocking ingest of huge corpora
   for simplicity; CLI covers the bulk load.
5. **HNSW vs. IVFFlat / exact search** — gave up insert speed and memory for query speed with
   no training step.

---

## 6. Measurements so far

| What | Value | Note |
|---|---|---|
| Docker image size | 2.6 GB | torch alone is 754 MB; ONNX Runtime would shrink this (next level) |
| Python import of torch + sentence-transformers | ~10 s | Why the server must answer the handshake before loading |
| Model load (bge-small, from baked cache) | 0.5 s | |
| Embed 64 short texts (CPU) | 0.3–0.8 s | Real chunk throughput measured in step 5 |
| Whole devportal repo, chunked (no embedding) | 814 files → **2,907 chunks**, 762k tokens | `includes/` inlined into parent pages |
| Tokens per chunk | p5 58 · p50 286 · p95 389 · **max 485** (limit 510) | Limit holds on the real corpus |
| Files with no chunks | 20 | All navigation-only pages (title + `tableofcontents`) |
| Chunking time, whole repo | 46 s | Dominated by repeated token counting; optimisation candidate |
| Embedding throughput (real chunks, CPU, batch 16–64) | **~4.4 chunks/s**; 4 vs 8 threads no difference | → full repo ≈ 11 min of embedding |
| **Full devportal ingest from GitHub (CLI)** | **688 s (11.5 min)**: 817 files → 817 sources, **2,910 chunks**, 762k tokens, **0 failures** | Clone + chunk + embed + write |
| **Re-ingest, nothing changed** | **12 s**, 817 × `unchanged`, 0 chunks written | Clone + hash only: the hash check works at scale |
| Database size after full ingest | 28 MB (incl. HNSW + GIN indexes) | Far from any Postgres limit |
| **Server over real `docker compose run -T` stdio** | handshake **7.9 s** (container start); first search **14.4 s** (waits for model load); then **~0.1 s** per search | Measured with a raw JSON-RPC probe against the full corpus |
| Re-ingest after restricting scans to MD/RST | 13 s: 814 unchanged, **3 `.txt` sources deleted by sync** → 814 sources, 2,907 chunks | Sync verified on real data |
| Query embedding latency | **141 ms** | Search will be dominated by this, not the DB |
| Biggest single source | `cassandra-metrics-prometheus.rst` → 181 chunks (6% of corpus) | A list of metric names; legit but dilutes top-k |

---

## 6b. Step 5 results (eval + load)

**Retrieval quality** — 30 questions ([eval/RESULTS.md](../eval/RESULTS.md)): 20 from randomly sampled pages, 10 targeted.

| Config | hit@1 | hit@5 | MRR@10 |
|---|---|---|---|
| vector only | 70% | 90% | 0.79 |
| keyword only (IDF) | 73% | 93% | 0.83 |
| **hybrid (RRF)** | **80%** | **97%** | **0.88** |
| hybrid + identifier boost | 80% | 97% | 0.88 |

Vector and keyword fail on *different* questions; hybrid recovers most. Caveat: n=30 (1 question = 3.3%).

**Load** (laptop, Docker VM 8 vCPU / 3.7 GiB, real corpus 2,907 chunks):

| What | Result |
|---|---|
| Search latency, sequential warm | p50 **38 ms**, p95 70 ms — embedding 29 ms, SQL 11 ms |
| Concurrency, one server process | **~14–19 req/s ceiling**; latency grows linearly: p50 51 ms (1 client) → 245 ms (4) → **1.1 s (16)** |
| Single-document ingest (sync tool path) | ~5 chunks/s: 10 KB 1.3–2.6 s, 100 KB 14–19 s, **1 MB 132–148 s** (743 chunks) |
| Whole repo ingest / unchanged re-ingest | 688 s / 12–13 s |
| Search while an ingest runs | before fix: p95 37 s, max 62 s → **after: p95 4.0 s, max 4.4 s** |
| Memory per server process (model loaded) | **375 MiB** (each Claude session spawns its own process) |

**Scale** (real chunks replicated with perturbed vectors, separate DB):

| Chunks | vector top-30 p50/p95 | keyword top-30 p50/p95 | full search p50/p95 | HNSW build | DB size |
|---|---|---|---|---|---|
| 2,907 (1×) | 0.9 / 3.0 ms | 5.8 / 10.8 ms | **8 / 13 ms** | 0.9 s | 28 MB |
| 29,070 (10×) | 2.2 / 7.8 ms | 102 / 230 ms | **130 / 415 ms** | 9.2 s | 201 MB |
| 145,350 (50×) | 2.1 / 6.7 ms | 512 / 830 ms | **507 / 809 ms** | 101 s | 963 MB |

**What breaks first, in order:**
1. **Embedding throughput** — CPU-bound model: ~5 chunks/s ingest, ~15 searches/s per process; latency grows linearly with concurrent clients. A 1 MB document blocks the synchronous `ingest` call for ~2.5 min (5 MB limit ≈ 12 min).
2. **Keyword side at data growth** — IDF counts matching chunks per term on every query: grows ~linearly with corpus size (6 → 102 → 512 ms) while HNSW stays ~2 ms. Fix: precomputed term statistics updated at ingest, or a BM25 index. (Worst case: copies share vocabulary exactly.)
3. **Memory per session** — 375 MiB per server process × concurrent Claude sessions.
4. **HNSW build time** — 101 s at 145k chunks with default `maintenance_work_mem`; matters for full rebuilds, not incremental inserts.

---

## 7. Assumptions (to copy into the README)

- "Whole repo" = all `.rst` and `.md` files; `includes/` is inlined into parent pages rather than
  stored separately; GitHub templates (`.github/`) are excluded.
- English-only content; full-text search uses the `simple` configuration.
- Single user / single server process; no auth.
- Public docs URLs are derived from file paths and may not resolve for every page (the repo is archived).

---

## 8. Build plan

1. ~~Project skeleton, Dockerfile, docker-compose, schema~~ ✅
2. ~~RST/MD parser + chunker + tests~~ ✅
3. ~~Embedder, ingest pipeline, idempotency, background jobs~~ ✅
4. ~~Hybrid search + MCP tools~~ ✅
5. ~~Eval set + load benchmark~~ ✅
6. README, then slide deck
