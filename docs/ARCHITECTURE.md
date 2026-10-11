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
| `ingest` accepts text, file path, directory, URL, and GitHub repo URL (shallow `git clone`) | Lets Claude ingest the whole repo with one call: `ingest("https://github.com/aiven/devportal")` | Needs `git` in the image |
| A directory/repo expands into **one `file` source per document** | `list_sources`/`delete_source`/idempotency stay per-page; citations are per-page | More rows in `sources` (fine at ~850) |
| Also a **CLI command** (`docker compose run --rm mcp-server ingest <path\|url>`) | Initial bulk load can exceed an MCP tool-call timeout; the CLI has no timeout | Two entry points (same code path underneath) |
| Resolve `.. include::` **inline into the parent page**; skip `includes/` as standalone sources | Parameters are cited under the real page (`kafka/reference/advanced-params.rst`); no duplicate chunks stealing top-k slots | Content included in several pages is stored once per page that includes it |
| `source_uri` = repo-relative path (+ derived public docs URL for devportal) | Container paths like `/tmp/clone/...` mean nothing to the user | URL derivation is devportal-specific (falls back to path) |
| Embed in batches (≈64) and bulk-insert chunks | Ingest throughput is dominated by embedding; batching is the cheap win | Higher peak memory during ingest |

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

### 3.7 Retrieval (search)

| Decision | Why | What we give up |
|---|---|---|
| **Hybrid search: vector top-30 + full-text top-30, fused with RRF (k=60)**, return top-k (default 5) | Test questions target exact tokens (parameter names, limits) where vector-only fails; RRF needs no score calibration | Two queries per search (still ms-level) |
| Optional `source_prefix` filter (e.g. `docs/products/kafka`) | Cheap metadata filter; helps Claude narrow ambiguous questions | — |
| Results return `content`, `source_uri`, docs URL, `heading_path`, scores | Claude can cite precisely; scores help debugging | Slightly larger tool responses |
| No reranker model in v1 | Adds latency + another model; hybrid gets most of the gain | Some precision left on the table (listed as "next level") |

### 3.8 MCP server & runtime

| Decision | Why | What we give up |
|---|---|---|
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
- **Idempotency:** same content → no re-embed; changed content → chunks replaced atomically;
  delete cascades.
- **Search:** hybrid finds an exact parameter name that vector-only misses; prefix filter works.
- **Integration:** MCP tools end to end against a real pgvector container.
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
| Biggest single source | `cassandra-metrics-prometheus.rst` → 181 chunks (6% of corpus) | A list of metric names; legit but dilutes top-k |

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
3. Embedder, ingest pipeline, idempotency
4. Hybrid search + MCP tools
5. Eval set + load benchmark
6. README, then slide deck
