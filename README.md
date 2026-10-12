# RAG MCP Server

An MCP server that lets Claude ingest documentation and answer questions over it with
retrieval (RAG). Built for the [aiven/devportal](https://github.com/aiven/devportal) docs
(~800 Markdown/RST pages, ~0.8M tokens), but it ingests any text, file, directory, web page or
GitHub repo.

- **Hybrid retrieval**: vector search (local `bge-small-en-v1.5`, no API key) + keyword search
  with IDF weighting, fused with Reciprocal Rank Fusion. Parameter names like `tcp_keepalives_idle`
  are matched exactly; paraphrased questions are matched semantically.
- **Structure-aware chunking** of RST/Markdown: every chunk knows its page and section
  (`Advanced parameters for Aiven for Apache Kafka® > private_access`), and Sphinx
  `.. include::` files are inlined into the pages that use them.
- **PostgreSQL + pgvector** in Docker Compose, data persisted in a volume.
- Measured: **97% of eval questions find the right page in the top 5**, search p50 **38 ms**
  ([eval results](eval/RESULTS.md), [measurements](#measured-performance--benchmarks)).

---

## Quickstart (about 10 minutes)

**You need:** Docker with Compose v2 (Docker Desktop on Mac/Windows), git, ~4 GB free disk,
≥ 4 GB RAM for Docker, and the [Claude Code](https://www.npmjs.com/package/@anthropic-ai/claude-code)
CLI (`npm install -g @anthropic-ai/claude-code`). No API keys.

### 1. Clone, build, start the database

```bash
git clone https://github.com/amitdako/MCP-REG-SERVER.git
cd MCP-REG-SERVER
docker compose build mcp-server
docker compose up -d db
```

The first build downloads CPU-only PyTorch and bakes the embedding model into the image
(measured on a fresh clone without cache: **~5.5 min**; later builds take seconds). The database schema is created automatically when the server first
starts (see [Database schema & initialization](#database-schema--initialization)).

> **Troubleshooting:** *Port 5433 already in use* means another Postgres is using that port. Start
> the database on a different one: `DB_PORT=5434 docker compose up -d db`.

### 2. Start ingesting the Aiven docs (runs in the background)

```bash
docker compose run -d --name rag-ingest mcp-server ingest https://github.com/aiven/devportal
```

This clones the repo and ingests all its Markdown/RST pages: 814 pages → 2,914 chunks in
**~11 minutes** on a laptop CPU (measured 10.8 min on a fresh clone; embedding runs on CPU).
Pages become searchable one by one as they are stored, so you can continue with step 3 now.
Follow progress with `docker logs -f rag-ingest` (Ctrl+C stops following, not the ingest).

> **Troubleshooting:** *Conflict. The container name "/rag-ingest" is already in use* means a
> previous ingest container still exists. Remove it with `docker rm rag-ingest`, then run the
> command again.

### 3. Add the server to Claude

The repo ships a pre-configured [`.mcp.json`](.mcp.json), so Claude Code finds the server with
no setup. This is the whole config, in case you want to paste it into another project's
`.mcp.json` (there, use `"-f", "/absolute/path/to/MCP-REG-SERVER/docker-compose.yml"` before
`"run"`, as in the [global setup](#claude-code-global-access)):

```json
{
  "mcpServers": {
    "rag-docs": {
      "command": "docker",
      "args": ["compose", "run", "--rm", "-T", "mcp-server"]
    }
  }
}
```

1. Go to the cloned folder: `cd MCP-REG-SERVER`
2. Start Claude Code: `claude`
3. Approve the **`rag-docs`** server when prompted. (`/mcp` now lists `rag-docs` as connected,
   with 5 tools.)
<img width="918" height="253" alt="image" src="https://github.com/user-attachments/assets/94d8bb46-faf1-4a8f-abb8-ed6549ae0c5c" />

Prefer a visual desktop app over the terminal? Use the [Claude Desktop App](#claude-desktop-app) setup instead.

> **Troubleshooting:** *`claude: command not found`* means the Claude Code CLI isn't installed or
> isn't on your `PATH`: `npm install -g @anthropic-ai/claude-code`. If **`rag-docs` isn't listed**,
> make sure Claude Code was started inside the cloned folder and that you approved the project
> server; `claude mcp list` shows its status.

### 4. Query the server (test)

Once the ingest from step 2 has finished (`docker logs rag-ingest` ends with a summary showing
`"processed": 814` and `"failed": 0`), ask in the same Claude session:

> What is the default tcp_keepalives_idle for Aiven for PostgreSQL?

Claude calls the server's `search` tool over stdio, gets the matching chunk from
`idle-connections.rst`, and answers **180 seconds**, citing the page's GitHub URL. The first search
of a session takes about 15 s while the embedding model loads; after that, searches take ~0.1 s.
More questions and real tool output: [Search](#search).

> **Troubleshooting:** if the answer doesn't cite the expected page, the ingest from step 2 is
> probably still running: pages become searchable as they are stored. Watch live progress with
> `docker logs -f rag-ingest`.

---

## Search

**You don't call `search` yourself.** Ask Claude a question in plain language, for example
*"What is the default tcp_keepalives_idle for PostgreSQL?"*, and Claude invokes the `search` MCP
tool behind the scenes, reads the returned chunks, and answers with the source cited. Calling
`search(...)` explicitly is optional, for when you want to control the parameters.

Under the hood, each search runs a **vector search** (meaning, so paraphrases work) and a
**keyword search** (exact terms like `tcp_keepalives_idle`, with rare terms weighted higher), and
merges the two rankings with Reciprocal Rank Fusion.

| Parameter | Required | Default | Meaning |
|---|---|---|---|
| `query` | **yes** | — | The question or keywords. |
| `top_k` | no | `5` | How many chunks to return (1–20). |
| `source_filter` | no | none | Only search sources whose URI contains this text, e.g. `"products/kafka"` or `"/data"`. |

**Naming the product helps.** Both sides of the hybrid search see each chunk's breadcrumb
(*Page > Section*), so a product named in the question pulls up the section about that product,
even when that section sits on a cross-product page (see Example 5); `source_filter` restricts the
search to that product's own pages.

### Examples

Every example below was run against the ingested devportal corpus; the retrieved source is the
actual first result.

#### Example 1: a configuration parameter

- **Question:** *"What is the default tcp_keepalives_idle for Aiven for PostgreSQL?"*
- **Retrieved source:** [`products/postgresql/reference/idle-connections.rst`](https://github.com/aiven/devportal/blob/main/docs/products/postgresql/reference/idle-connections.rst)
  (vector #1, keyword #1)
- **Expected answer:** `180` seconds.

#### Example 2: a calculation from a rule

- **Question:** *"How many static IP addresses do I need for a 9 node plan?"*
- **Retrieved source:** [`platform/howto/static-ip-addresses.rst`](https://github.com/aiven/devportal/blob/main/docs/platform/howto/static-ip-addresses.rst)
  (vector #1, keyword #1)
- **Expected answer:** `15` static IPs: plans with more than 6 nodes need the number of nodes + 6.

#### Example 3: a how-to

- **Question:** *"How do I enable automatic topic creation in Kafka?"*
- **Retrieved source:** [`products/kafka/howto/create-topics-automatically.rst`](https://github.com/aiven/devportal/blob/main/docs/products/kafka/howto/create-topics-automatically.rst)
- **Expected answer:** It is disabled by default. Enable it in the Aiven Console
  (**Service settings → Advanced configuration**, set `auto_create_topics_enable` to `true`) or
  with the Aiven CLI.

#### Example 4: what a parameter does

- **Question:** *"What does auto_create_topics_enable do?"*
- **Retrieved source:** [`products/kafka/howto/create-topics-automatically.rst`](https://github.com/aiven/devportal/blob/main/docs/products/kafka/howto/create-topics-automatically.rst),
  section *Enable automatic topic creation using Aiven Console*
- **Expected answer:** Set to `true`, Kafka creates a topic automatically when a message is
  produced to a topic that doesn't exist yet.

#### Example 5: a product named in a general topic

- **Question:** *"How often does Aiven for ClickHouse take backups?"*
- **Retrieved source:** [`platform/concepts/service_backups.rst`](https://github.com/aiven/devportal/blob/main/docs/platform/concepts/service_backups.rst),
  section *Backup profile per service > Aiven for ClickHouse®*
- **Expected answer:** Daily, automatically: backups are incremental, encrypted and stored in
  object storage in the same region.
- With `source_filter="products/clickhouse"`, the first result is
  [`products/clickhouse/concepts/disaster-recovery.rst`](https://github.com/aiven/devportal/blob/main/docs/products/clickhouse/concepts/disaster-recovery.rst)
  (*Backup and restore*), which gives the same answer.

#### Example 6: a limit that depends on the plan

- **Question:** *"How many connections per GiB of RAM does Aiven for MySQL allow?"*
- **Retrieved source:** [`products/mysql/concepts/max-number-of-connections.rst`](https://github.com/aiven/devportal/blob/main/docs/products/mysql/concepts/max-number-of-connections.rst)
- **Expected answer:** 75 per GiB on plans under 4 GiB of RAM, 100 per GiB from 4 GiB, plus 1
  extra connection for the system process.

#### Example 7: an eligibility requirement

- **Question:** *"What minimum monthly spend is required for Bring Your Own Cloud?"*
- **Retrieved source:** [`platform/concepts/byoc.rst`](https://github.com/aiven/devportal/blob/main/docs/platform/concepts/byoc.rst),
  section *Who is eligible for BYOC* (vector #1, keyword #1)
- **Expected answer:** At least $5,000 per month, for at least 12 months.

<details>
<summary>What the <code>search</code> tool actually returns to Claude (Example 1, trimmed)</summary>

```
[1] Keep-alive connections parameters
source: https://github.com/aiven/devportal/blob/main/docs/products/postgresql/reference/idle-connections.rst
score: 0.0328 (vector #1, keyword #1)

## Keep-alive server side parameters
| Parameter (server) | Value | Description |
|`tcp_keepalives_idle` |180 |Specifies the amount of time with no network activity after which the |
| | |operating system should send a TCP `keepalive` message to the client.|
...
```

Each result has a breadcrumb (`Page > Section`), the source URL to cite, the chunk's rank on
each side of the hybrid search, and the chunk text.
</details>

## Ingesting content

**Inside Claude, just ask.** For example *"Ingest /data/notes.md"* or
*"Ingest https://github.com/aiven/devportal"*: Claude invokes the `ingest` tool for you. The
terminal command `docker compose run --rm mcp-server ingest <path or URL>` is an optional
standalone path, with no Claude session needed (step 2 of the quickstart uses it).

### Inputs

`ingest(text | path | url, name?)` takes exactly one input:

| Input | What it accepts | Example request to Claude |
|---|---|---|
| `text` | Raw text or Markdown. An optional `name` gives it a stable ID (`text:<name>`), so ingesting again under the same name replaces it. | *"Ingest this as 'team-notes': ..."* |
| `path` | A file or folder on your machine, placed in the repo's **`./data`** folder, which the container sees as **`/data/...`** (read-only). Files: `.md` `.markdown` `.mdx` `.rst` `.txt`; folders: Markdown and RST files. | *"Ingest /data/notes.md"*, *"Ingest /data"* |
| `url` | A web page (converted to Markdown), a raw `.md`/`.rst`/`.txt` file, a GitHub file, or a GitHub repo or folder. | *"Ingest https://github.com/aiven/devportal"* |

Each document can be up to 5 MB.

### How it runs

- **Single documents and text** are ingested synchronously: the call returns the result
  (`created`, `updated` or `unchanged`, with the chunk count) as soon as the document is stored.
- **Folders and GitHub repos** run asynchronously as a background job: the call returns a job ID
  immediately, documents become searchable one by one, and `ingest_status(job_id)` reports
  progress, ETA and any errors. Jobs live in the server process; if the Claude session ends,
  run the same ingest again and finished documents are skipped.

### Idempotency

Every document is hashed on **content + chunker version + embedding model**, so ingesting the same
input again is safe and cheap:

- **Unchanged files** are skipped instantly, with zero embedding cost (all of devportal
  re-ingests in 12 s).
- **Changed files** are replaced atomically: old chunks are removed and new ones written in a
  single database transaction, so searches never see a half-updated document.
- **Deleted files** are cleaned up automatically when a repo or folder is re-ingested in full
  (only after a run with no failures, so a file that merely failed to load is never deleted).

Because the chunker version and model are part of the hash, upgrading either one re-embeds
everything on the next ingest.

## Managing sources

### Inspect what's ingested: `list_sources`

Ask Claude in plain language, for example *"Show me all ingested sources"* or *"Which Kafka pages
are ingested?"*, or call `list_sources(filter?, limit=50, offset=0)` directly.

Every response starts with **global stats** (total sources, chunks and tokens, the count per source
type, and any running ingest jobs), followed by **one page of sources**, newest first, each with its
title, URI, chunk count and last update.

| Parameter | Default | Meaning |
|---|---|---|
| `filter` | none | Only sources whose URI or title contains this text (case-insensitive), e.g. `"kafka"`. |
| `limit` | `50` | Sources per page (max 200). Paging keeps responses short enough for Claude's context: the full devportal corpus is 814 sources. |
| `offset` | `0` | Sources to skip. When more exist, the response says which `offset` to use next. |

### Delete a source: `delete_source`

Ask Claude to delete a source by its **exact URI**, as shown by `list_sources` or in search results,
for example *"Delete the source text:team-notes"*. This calls `delete_source(source_uri)`.

- **One step, both indexes.** The delete removes the source row, and `ON DELETE CASCADE` removes all
  of its chunks in the same statement. Each chunk row holds both the vector embedding and the
  keyword index entry, so the source disappears from both sides of search at once.
- **Exact match only.** There are no wildcards or prefixes, so one call can never remove more than
  the one source you named. An unknown URI returns an error pointing back to `list_sources`.
- A file deleted from an ingested repo or folder comes back the next time that repo or folder is
  re-ingested, because it still exists upstream.

### Reset everything (terminal)

To wipe the whole database at once, run `docker compose down -v` in the repo folder: it deletes the
data volume, and the next server start recreates an empty schema.

---

## Database schema & initialization

Database initialization is automatic and idempotent. On every start, the server runs
[`schema.sql`](src/rag_mcp/schema.sql) under a Postgres advisory lock with `IF NOT EXISTS` guards,
so it works on a fresh volume and on an existing database, and two servers starting at the same
time can't race each other.

* **Automatic setup:** no manual database step is needed. To run the schema step on its own (it
  only creates what is missing and never deletes data):
  ```bash
  docker compose run --rm mcp-server init-db
  ```
* **Tables:** `sources` (one row per document: URI, type, title, content hash, chunk and token
  counts) and `document_chunks` (chunk text, the breadcrumb-enriched text that gets embedded, a
  `vector(384)` embedding and a generated `tsvector` for keyword search).
* **Indexes:** HNSW (cosine) on the embeddings, GIN on the `tsvector`, and one on `source_id` for
  fast cascading deletes.
* **Safety check:** startup stops with a clear message if the configured embedding model's
  dimension doesn't match the existing `vector` column (for example, after switching models on an
  old volume).
* **Persistence:** data lives in the `pgdata` Docker volume and survives restarts and rebuilds.
  `docker compose down -v` deletes it.

## Configuration

**No environment variables are required.** All have working defaults (set them in the
`environment:` section of `mcp-server` in `docker-compose.yml`):

| Variable | Default | Meaning |
|---|---|---|
| `DATABASE_URL` | `postgresql://rag:rag@db:5432/rag` | Postgres connection |
| `EMBEDDING_MODEL` | `BAAI/bge-small-en-v1.5` | Must be baked into the image (build arg of the same name) |
| `EMBEDDING_DIM` | `384` | Must match the model; checked at startup |
| `INGEST_BATCH_SIZE` | `16` | Chunks per embedding batch (a search waits for at most one batch) |
| `LOG_LEVEL` | `INFO` | Logs go to stderr (stdout is the MCP protocol) |
| `DB_PORT` | `5433` | Host port of Postgres (`127.0.0.1` only), for debugging |

---

## Tests, eval and benchmarks

```bash
docker compose run --rm tests                      # 82 tests, ~2 min (uses a separate rag_test DB)
docker compose run --rm --entrypoint python mcp-server scripts/eval_retrieval.py   # retrieval eval
docker compose run --rm --entrypoint python mcp-server scripts/benchmark.py latency  # or: concurrency | ingest | scale
```

What the tests cover, and why:
- **Chunking** with the real tokenizer — no chunk ever exceeds the model's 512-token limit
  (anything beyond would silently not be embedded), no text is lost, RST edge cases.
- **Ingest** — unchanged documents cost zero embedding calls, a failed write keeps the old
  version, sync never deletes after a partial run.
- **Search** — regression tests for real bugs found on the corpus (rare identifiers losing to
  common words; searches starved during an ingest).
- **End to end** — the real server spawned over stdio and driven with the MCP client.

The **retrieval eval** ([eval/RESULTS.md](eval/RESULTS.md)) has 30 questions — 20 from randomly
sampled pages, 10 on parameters/limits/how-tos — and compares vector-only, keyword-only and hybrid.

## Architecture

```
Claude ──stdio──► mcp-server (one Python process per Claude session)
                   ingest ─► load (text/file/dir/URL/GitHub) ─► parse RST/MD into a section tree
                             ─► chunk (≤350 tokens, breadcrumbs) ─► embed (bge-small, CPU) ─► Postgres
                   search ─► embed query ─┬─► HNSW vector top-30 ─┐
                                          └─► keyword top-30 (IDF) ┴─► RRF ─► top 5 with sources
```

Components, both pipelines and the storage layout are described in
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Measured performance & benchmarks

Benchmarked on a local laptop environment (**Docker VM: 8 vCPUs / 3.7 GiB RAM**). Reproduce with
`scripts/benchmark.py`; retrieval quality is in [eval/RESULTS.md](eval/RESULTS.md).

### Ingestion metrics

| Scenario | Scale | Time / throughput | Key takeaway |
| :--- | :--- | :--- | :--- |
| **Initial ingest** | 814 pages (2,914 chunks) | ~11.5 min (~5 chunks/s) | CPU-bound embedding generation. |
| **Re-ingest (unchanged)** | 814 pages | **12 seconds** | Fast hash-based idempotency skip. |

### Search performance (standard corpus)

* **Search latency (p50):** `38 ms` *(median of each part: embedding 29 ms, SQL hybrid query 11 ms)*
* **Search latency (p95):** `70 ms`
* **Throughput:** `~15 searches/sec` (single server process)
* **Memory overhead:** `~375 MiB` per process

### Scaling benchmark (50× corpus, 145,000 chunks)

* **Search latency (p50):** `507 ms`
* **Bottleneck analysis:** vector retrieval (HNSW) stays fast at scale (`2.1 ms` p50); keyword
  ranking is the primary latency factor (`512 ms` p50), because its IDF weights are computed per
  query by counting the matching chunks for every term, which grows with the corpus.

## Assumptions

- "Ingest the whole repo" = every Markdown/RST file (`.md .markdown .mdx .rst`), except
  `.github/`, and except files that other files `.. include::` — those are inlined into the
  including pages instead of stored twice. Other files (`requirements.txt`, ...) are not docs.
- The repo is archived (last commit Jan 2024); sources are cited by their GitHub URL.
- English content; one user; no authentication (local stdio server).
- A single Postgres container and one server process per Claude session (as the task allows).
- "One command brings up both the database and the server": with stdio, Claude starts the
  server itself, so it can't be a long-running `docker compose up` service (it sits under a
  compose profile, and `docker compose up` alone starts only Postgres). The one command is the
  MCP config's `docker compose run --rm -T mcp-server`: through `depends_on` it also creates the
  volume, starts Postgres, waits for its healthcheck, and the server creates the schema. Step 1's
  `docker compose up -d db` only starts the database early so the ingest can run first.

## Known limitations & trade-offs

* **CPU embedding overhead:** ingestion runs on the CPU at ~5 chunks/sec. A single large document
  is processed synchronously and blocks the tool call (a 1 MB document takes ~2.5 min), whereas
  directories and repositories are handled as asynchronous background jobs.
* **In-memory job state:** background ingest jobs are bound to the active session's server process.
  If the session ends mid-job, re-running the same ingest resumes the work, skipping files that
  are already stored via hash comparison.
* **Cold start latency:** the first query of a new session waits ~10–15 s while the embedding
  model loads into memory. Later queries take ~40 ms median (p95 70 ms).
* **Keyword search scaling:** keyword scoring computes its IDF weights per query, so its cost grows
  roughly linearly with corpus size: 6 ms → 102 ms → 512 ms p50 at 1× / 10× / 50× (145,000
  chunks), while vector search stays ~2 ms. The fix is precomputed term statistics or a BM25 index.
* **Scope & media:** no re-ranking model (kept out to save CPU latency), no JavaScript execution
  on scraped web pages, and text only (images are not indexed).

## Alternative setups

The quickstart uses Claude Code in the terminal with the repo's `.mcp.json`. Choose one of these
instead if you prefer the visual desktop app, or want the server available in every Claude Code
session.

### Claude Desktop App

**Before you start:** steps 1–2 of the quickstart are done, Docker Desktop is running, and you know
the absolute path of the cloned folder (for example `C:\Users\you\MCP-REG-SERVER`).

1. **Open the config file.** In Claude Desktop go to **Settings → Developer → Edit Config**, then
   open `claude_desktop_config.json` in a text editor:
   - Windows: `%APPDATA%\Claude\claude_desktop_config.json`
   - macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`

2. **Add the `rag-docs` server.** The file usually already contains other settings; keep them and
   add the `"mcpServers"` block after them, separated by a comma:

   ```json
   {
     "preferences": { "...": "existing settings stay as they are" },
     "mcpServers": {
       "rag-docs": {
         "command": "docker",
         "args": ["compose", "-f", "C:/Users/you/MCP-REG-SERVER/docker-compose.yml",
                  "run", "--rm", "-T", "mcp-server"]
       }
     }
   }
   ```

   - **Empty file or `{}`:** use the block above without the `"preferences"` line.
   - **`"mcpServers"` already exists:** add only the `"rag-docs": { ... }` entry inside it, with a
     comma between it and the previous server.

3. **Set your path.** Replace `C:/Users/you/MCP-REG-SERVER` with the absolute path of your folder,
   keeping `/docker-compose.yml` at the end. Use **forward slashes** (`/`) on both Windows and
   macOS: a single backslash is invalid in JSON. macOS example:
   `/Users/you/MCP-REG-SERVER/docker-compose.yml`.

4. **Restart fully.** Save the file and quit Claude Desktop completely (Windows: system tray icon
   → **Quit**; macOS: **Cmd+Q**); closing the window is not enough. Then reopen it.

5. **Check.** **Settings → Developer** lists `rag-docs` without an error. A config error usually
   means invalid JSON, most often a missing or extra comma.

> **Troubleshooting:** if `rag-docs` shows as **failed**, check that Docker Desktop is running and
> that the path points to the `docker-compose.yml` **file**, written with forward slashes (`/`).
> To test it outside Claude, run the configured command in a terminal: it should wait silently for
> input (Ctrl+C to stop).

**How to use:** open a new chat and ask in plain language; there are no function calls or flags
to type. For example *"How often does ClickHouse take backups?"*, *"Ingest /data/notes.md"* or
*"Show me all ingested sources"*. Claude calls the right `rag-docs` tool in the background and
answers with the source cited. Files you want to ingest go in the repo's `./data` folder, which
the server sees as `/data/...`.

### Claude Code (Global Access)

The repo's `.mcp.json` makes the server available only when Claude Code runs inside the cloned
folder. To use it from any folder, register it once. Run the command for your shell **from inside
the cloned folder**; it fills in the absolute path for you:

```bash
# macOS / Linux
claude mcp add rag-docs -- docker compose -f "$(pwd)/docker-compose.yml" run --rm -T mcp-server
```

```powershell
# Windows PowerShell
claude mcp add rag-docs -- docker compose -f "$PWD\docker-compose.yml" run --rm -T mcp-server
```

```bash
# Windows Git Bash (pwd -W prints a Windows path such as C:/Users/...)
claude mcp add rag-docs -- docker compose -f "$(pwd -W)/docker-compose.yml" run --rm -T mcp-server
```

Then start `claude` in any folder; `/mcp` lists `rag-docs` as connected, with 5 tools.

> **`claude: command not found`?** The Claude Code CLI isn't installed or isn't on your `PATH`.
> Install it with `npm install -g @anthropic-ai/claude-code`, or use the
> [Claude Desktop App](#claude-desktop-app) setup above if you prefer a visual app.

> **Troubleshooting (Windows):** Git Bash rewrites arguments that start with `/`, so a command like
> `docker compose run --rm mcp-server ingest /data/notes.md` fails with *no such file or directory
> ... C:/Program Files/Git/data/...*. Prefix such commands with `MSYS_NO_PATHCONV=1`, or run them in
> PowerShell. The registration commands above and ingesting through Claude are not affected.

**How to use:** ask in plain language, exactly as in the desktop app: *"Ingest /data/notes.md"*,
*"How often does ClickHouse take backups?"*, *"Show me all ingested sources"*. Claude Code picks
and calls the `rag-docs` tools itself.

---

## Quick troubleshooting index

| Error or symptom | Where it's explained |
|---|---|
| `Port 5433 already in use` | [Quickstart step 1](#1-clone-build-start-the-database) |
| `Conflict. The container name "/rag-ingest" is already in use` | [Quickstart step 2](#2-start-ingesting-the-aiven-docs-runs-in-the-background) |
| `claude: command not found` | [Quickstart step 3](#3-add-the-server-to-claude) |
| `rag-docs` not listed in Claude Code | [Quickstart step 3](#3-add-the-server-to-claude) |
| Answer doesn't cite the expected page | [Quickstart step 4](#4-query-the-server-test) |
| `rag-docs` shows as failed (Docker, path) | [Claude Desktop App](#claude-desktop-app) |
| `no such file or directory ... C:/Program Files/Git/...` | [Claude Code (Global Access)](#claude-code-global-access) |

To wipe all data and start completely fresh: `docker compose down -v`.
