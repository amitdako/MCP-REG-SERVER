# RAG MCP Server — Architecture

How the `rag-docs` MCP server is put together: its components, the two pipelines (ingest and
search), and how data is stored. For setup and usage, see the [README](../README.md).

---

## 1. System Components

```
 ┌──────────────────────────┐
 │ Claude Code / Desktop    │   the user asks in plain language;
 │                          │   Claude picks and calls the tools
 └────────────┬─────────────┘
              │ stdio (JSON-RPC)  ·  one server process per Claude session
              ▼
 ┌──────────────────────────────────────────────────────────────────┐
 │ MCP Server  (Python, MCP SDK 2.x, Docker container)              │
 │                                                                  │
 │  tools:  ingest · ingest_status · search · list_sources ·        │
 │          delete_source                                           │
 │                                                                  │
 │  ┌──────────┐   ┌──────────┐   ┌──────────┐   ┌───────────────┐  │
 │  │ Loader   │──►│ Parser   │──►│ Chunker  │──►│ Embedder      │  │
 │  │ text,    │   │ RST, MD, │   │ token-   │   │ bge-small-en- │  │
 │  │ file,    │   │ HTML →   │   │ based,   │   │ v1.5, local   │  │
 │  │ dir, URL,│   │ section  │   │ ≤350 tok │   │ CPU, 384-dim  │  │
 │  │ GitHub   │   │ tree     │   │          │   │               │  │
 │  └──────────┘   └──────────┘   └──────────┘   └───────┬───────┘  │
 │                                                       │          │
 │  Searcher: hybrid vector + keyword, RRF fusion ◄──────┤          │
 └───────────────────────────────────────────────────────┼──────────┘
                                                         ▼
 ┌──────────────────────────────────────────────────────────────────┐
 │ PostgreSQL 16 + pgvector  (Docker container, `pgdata` volume)    │
 │   sources ──1:N──► document_chunks                               │
 │   HNSW index (vectors)  ·  GIN index (full-text)                 │
 └──────────────────────────────────────────────────────────────────┘
```

- **Transport.** stdio: Claude spawns the server with `docker compose run --rm -T mcp-server`.
  stdout carries only the protocol; all logs go to stderr.
- **Start-up.** The server answers the MCP handshake at once and loads the embedding model in a
  background thread. Tools that need the model wait until it is ready.
- **Offline.** The model is baked into the image at build time, so it needs no network and no API key.

---

## 2. Pipeline Hierarchy

### 2.1 Ingestion pipeline

```
 ingest(text | path | url)
   │
   ├─ directory / GitHub repo ──► background job: returns a job_id at once,
   │                              ingest_status reports progress; each file
   │                              below runs on its own and commits separately
   ▼
 1. Load ──► 2. Hash ──► 3. Parse & Tree ──► 4. Chunk & Pack ──► 5. Batch Embed ──► 6. Atomic Store
               │
               └─ unchanged? ──► skip (no parsing, no embedding)
```

**1. Load.** The input becomes a `Document`, identified by its `source_uri`:

| Input | `source_uri` |
|---|---|
| raw text | `text:<name>` |
| local file | `/data/...` |
| web page or raw file URL | the URL |
| file from a GitHub repo | its GitHub blob URL |

Web pages are converted to Markdown. RST `.. include::` directives are **resolved inline**: the
included file's text is placed inside the page that includes it, nested up to 5 levels. Files
that are only ever included are not stored as separate sources.

**2. Hash.** SHA-256 over the content, the chunker version and the embedding model name. If it
matches the stored hash, the document is reported `unchanged` and skipped. Re-ingesting an
unchanged repository therefore costs no embedding work.

**3. Parse & Tree.** A lightweight RST parser (Markdown uses ATX `#` headings) builds a tree of
headed sections:

- **Headings.** A text line underlined by one repeated punctuation character. The level is the
  order in which each underline style first appears.
- **Cleanup.**
  - Substitutions (`.. |x| replace::`) are resolved.
  - Code blocks are kept verbatim as fenced code.
  - Table text is kept; `list-table` rows become one self-contained line each.
  - Notes and warnings keep their content.
  - Images, TOCs, comments and labels are dropped.
  - Inline roles and links are reduced to their text.

**4. Chunk & Pack.** Chunking follows the section structure and counts tokens with the model's own
tokenizer.

- **Sizes:** target 350 / max 450 / overlap 50 tokens.
- **Packing:** small sibling sections are packed together up to 350 tokens, never across a
  parent boundary.
- **Splitting:** oversized sections are split by paragraph, then line, then token window. Each
  piece repeats the section heading.
- **Breadcrumb:** each chunk gets one, e.g. `[Kafka advanced parameters > private_access]`. It
  is added **only to `embedding_input`** (the text that is embedded and keyword-indexed).
  `content`, the text Claude reads, stays clean.
- **Limit:** breadcrumb + content is guaranteed to fit the model's 512-token limit.

**5. Batch Embed.** Chunks are embedded in batches of 16 into normalized 384-dim vectors. The model
runs one call at a time, with **priority locking**:

- Before each batch, an ingest checks for waiting searches and gives way to them.
- A search during a large ingest waits for at most the one batch already running.
- Embedding happens before any database transaction, so no locks are held during CPU work.

**6. Atomic Store.** One transaction per document:

```
pg_advisory_xact_lock(source_uri)  ──►  upsert sources row  ──►  DELETE old chunks  ──►  INSERT new chunks  ──►  COMMIT
```

Readers see either the old version or the new one, never a mix.

**Sync after a full directory or repo walk.** Sources under that prefix that no longer exist are
deleted. This happens only if no file failed during the walk.

### 2.2 Hybrid search pipeline

```
 query
   │
   ▼
 embed (bge query prefix, priority over ingest)
   │
   ├──────────────────────────────────┐
   ▼                                  ▼
 Vector top-30                      Keyword top-30
 HNSW, cosine distance              GIN on tsvector, terms OR-matched,
 (semantic match)                   ranked by IDF (rare terms weigh most)
   │                                  │
   └───────────────┬──────────────────┘
                   ▼
 RRF fusion:  score = 1/(60 + vector_rank) + 1/(60 + keyword_rank)
                   │
                   ▼
 top_k (default 5) ──► Markdown response ──► Claude answers and cites the source URL
```

- **Query terms.** Stopwords are dropped and every other term is matched on its own (OR).
  Identifiers like `max_connections` are matched as exact phrases.
- **Keyword ranking.** Each chunk scores the sum of the BM25-style IDF weights of the terms it
  contains:

  ```
  idf = ln(1 + (N − df + 0.5) / (df + 0.5))
  ```

  The document frequency (`df`) of each term is counted through the GIN index on every query.
- **Fusion.** Reciprocal Rank Fusion combines the two lists by rank, so cosine distances and
  keyword scores never need to be put on one scale.
- **One SQL query.** Both retrievals and the fusion run in a single query. Each side takes its
  top 30 in a subquery, so the vector side can use the HNSW index.
- **Source filter.** An optional `source_filter` restricts both sides to sources whose URI
  contains the given text.
- **Response.** Each hit comes back as:

  ```
  [n] Page > Section
  source: https://github.com/aiven/devportal/blob/main/docs/...
  score: 0.0328 (vector #1, keyword #1)

  …chunk content…
  ```

---

## 3. Data & Storage Hierarchy

### 3.1 Schema

```
 sources                                     document_chunks
 ─────────────────────────────               ──────────────────────────────────────────
 id             PK                ──1:N──►   id                PK
 source_uri     UNIQUE                       source_id         FK → sources.id
 source_type    file | url | text                              ON DELETE CASCADE
 title                                       chunk_index       UNIQUE (source_id, chunk_index)
 content_hash   SHA-256                      heading_path      TEXT[]  (breadcrumb)
 chunk_count                                 content           text shown to Claude
 total_tokens                                embedding_input   breadcrumb + content
 metadata       JSONB                        token_count
 created_at / updated_at                     metadata          JSONB
                                             embedding         vector(384)
                                             content_tsv       tsvector, GENERATED from
                                                               embedding_input ('simple')
```

### 3.2 Storage mechanics

| Mechanism | What it does |
|---|---|
| **HNSW index** on `embedding` (`vector_cosine_ops`) | Approximate nearest-neighbour search by cosine distance, with no training step |
| **GIN index** on `content_tsv` | Fast keyword matching. The `simple` configuration (no stemming) matches identifiers exactly. Titles and breadcrumbs are included because the column is generated from `embedding_input` |
| **Index on `source_id`** | Fast per-source deletes and re-ingests |
| **`ON DELETE CASCADE`** | Deleting a source removes all its chunks, from both the vector and the keyword index, in one statement |
| **Per-URI advisory lock** (`pg_advisory_xact_lock`) | Serializes concurrent ingests of the same `source_uri`; held only for the short write transaction |
| **Single-transaction replace** | Upsert source, delete old chunks and insert new ones commit together |
| **Schema at startup** | `schema.sql` runs idempotently (`IF NOT EXISTS`) under an advisory lock. Startup also checks that the `vector(N)` column matches the configured model's dimension, and stops with a clear message if it doesn't |
| **Persistence** | Data lives in the `pgdata` Docker volume and survives restarts and rebuilds. `docker compose down -v` resets it |
