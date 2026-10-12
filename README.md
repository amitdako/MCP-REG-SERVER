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
  ([results](eval/RESULTS.md), [all measurements](docs/ARCHITECTURE.md#6b-step-5-results-eval--load)).

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
starts (see [How the schema is created](#how-the-schema-is-created)).

### 2. Start ingesting the Aiven docs (runs in the background)

```bash
docker compose run -d --name rag-ingest mcp-server ingest https://github.com/aiven/devportal
```

This clones the repo and ingests all its Markdown/RST pages: 814 pages → 2,907 chunks in
**~11 minutes** on a laptop CPU (measured 10.8 min on a fresh clone; embedding runs on CPU).
Pages become searchable one by one as they are stored, so you can continue with step 3 now.
Follow progress with `docker logs -f rag-ingest` (Ctrl+C stops following, not the ingest).

### 3. Add the server to Claude

The repo ships a pre-configured [`.mcp.json`](.mcp.json), so Claude Code finds the server with
no setup.

1. Go to the cloned folder: `cd MCP-REG-SERVER`
2. Start Claude Code: `claude`
3. Approve the **`rag-docs`** server when prompted. (`/mcp` now lists `rag-docs` as connected,
   with 5 tools.)
<img width="918" height="253" alt="image" src="https://github.com/user-attachments/assets/94d8bb46-faf1-4a8f-abb8-ed6549ae0c5c" />

### 4. Query the server (test)

Once the ingest from step 2 has finished (`docker logs rag-ingest` ends with a summary showing
`"processed": 814` and `"failed": 0`), ask in the same Claude session:

> What is the default tcp_keepalives_idle for Aiven for PostgreSQL?

Claude calls the server's `search` tool over stdio, gets the matching chunk from
`idle-connections.rst`, and answers **180 seconds**, citing the page's GitHub URL. The first search
of a session takes about 15 s while the embedding model loads; after that, searches take ~0.1 s.
More questions and real tool output: [Search](#search).

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

## How the schema is created

There is no manual step. On every start the server runs [`schema.sql`](src/rag_mcp/schema.sql)
idempotently (`CREATE EXTENSION/TABLE/INDEX IF NOT EXISTS`, under a Postgres advisory lock),
so it works on a fresh volume and on an existing one. You can also run it explicitly:
`docker compose run --rm mcp-server init-db`.

Tables: `sources` (one row per document: URI, type, title, content hash, counts) and
`document_chunks` (text, breadcrumb-enriched embedding input, `vector(384)`, generated
`tsvector`), with an HNSW index (cosine) and a GIN full-text index. Data lives in the
`pgdata` Docker volume and survives restarts; `docker compose down -v` deletes it.

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
docker compose run --rm tests                      # 78 tests, ~1.5 min (uses a separate rag_test DB)
docker compose run --rm --entrypoint python mcp-server scripts/eval_retrieval.py   # retrieval eval
docker compose run --rm --entrypoint python mcp-server scripts/benchmark.py latency  # or: concurrency | ingest | scale
```

What the tests cover, and why ([details](docs/ARCHITECTURE.md#4-testing-strategy-what-we-test-and-why)):
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

Every design decision, with why and what it gives up, is in
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Measured performance (laptop, Docker VM 8 vCPU / 3.7 GiB)

| | |
|---|---|
| Full devportal ingest | 814 pages → 2,907 chunks in ~11.5 min (~5 chunks/s, CPU embedding) |
| Re-ingest, nothing changed | 12 s |
| Search latency | p50 38 ms, p95 70 ms (embedding 29 ms, SQL 11 ms) |
| Throughput, one server process | ~15 searches/s |
| Memory per server process | 375 MiB |
| 50× corpus (145k chunks) | search p50 507 ms — the keyword side is the first to slow down |

## Assumptions

- "Ingest the whole repo" = every Markdown/RST file (`.md .markdown .mdx .rst`), except
  `.github/`, and except files that other files `.. include::` — those are inlined into the
  including pages instead of stored twice. Other files (`requirements.txt`, ...) are not docs.
- The repo is archived (last commit Jan 2024); sources are cited by their GitHub URL.
- English content; one user; no authentication (local stdio server).
- A single Postgres container and one server process per Claude session (as the task allows).

## Known limitations

- **Embedding on CPU is the bottleneck**: ~5 chunks/s to ingest; a single document ingested
  synchronously blocks the tool call (1 MB ≈ 2.5 min). Directories/repos run as background jobs.
- **Ingest jobs live in memory**: if the Claude session ends, the job stops; re-run the same
  ingest and it resumes (unchanged pages are skipped).
- The first search of a session waits for the model to load (~10–15 s).
- Keyword scoring counts matching chunks per query; it slows down roughly linearly with corpus
  size (fine here, ~0.5 s at 50×).
- No reranker; JavaScript-rendered web pages are not executed; images are not indexed.

## Troubleshooting

- **`claude: command not found`:** the Claude Code CLI isn't installed or on your PATH —
  `npm install -g @anthropic-ai/claude-code`, or open the cloned folder in the Claude desktop
  app's **Code** tab.
- **`rag-docs` not listed in Claude Code:** start Claude Code inside the cloned folder (step 3),
  and approve the project server when prompted (or check `claude mcp list`).
- **Claude shows the server as failed:** check that Docker is running. With one of the
  alternative setups below, also check that the path points to the `docker-compose.yml`
  **file** (in bash, `\` is an escape character — use `/`). Run the configured command yourself:
  it should wait silently for input (Ctrl+C to stop).
- **Claude answers without the right page:** the ingest from step 2 may still be running —
  `docker logs rag-ingest` shows its progress; pages become searchable as they are stored.
- **"Conflict. The container name "/rag-ingest" is already in use":** a previous ingest
  container exists — `docker rm rag-ingest`, then run step 2 again.
- **Port 5433 already in use:** `DB_PORT=5434 docker compose up -d db`.
- **Git Bash on Windows: `no such file or directory ... C:/Program Files/Git/data/...`:**
  Git Bash rewrites arguments starting with `/`. Prefix the command with `MSYS_NO_PATHCONV=1`
  (or use PowerShell). Ingesting through Claude is not affected.
- **Start over:** `docker compose down -v` deletes all ingested data.

---

## Alternative: Claude Desktop App

Use this instead of steps 3–4 to connect the **Claude Desktop chat app**.

**Before you start:** steps 1–2 are done, Docker Desktop is running, and you know the absolute
path of the cloned folder (for example `C:\Users\you\MCP-REG-SERVER`).

1. **Open the config file.** In Claude Desktop go to **Settings → Developer → Edit Config**, then
   open `claude_desktop_config.json` in a text editor:
   - Windows: `%APPDATA%\Claude\claude_desktop_config.json`
   - macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`

2. **Add the server entry.** This is the block to add:

   ```json
   "rag-docs": {
     "command": "docker",
     "args": ["compose", "-f", "C:/Users/you/MCP-REG-SERVER/docker-compose.yml",
              "run", "--rm", "-T", "mcp-server"]
   }
   ```

   Where it goes depends on what the file already contains:

   | If the file… | Do this |
   |---|---|
   | is empty or contains only `{}` | Replace its contents with the complete file below, without the `"preferences"` line. |
   | has other settings but no `"mcpServers"` | Keep everything. Add a comma after the last existing entry, then add the `"mcpServers": { ... }` part before the final `}`. |
   | already has `"mcpServers"` | Add only the `"rag-docs": { ... }` block inside it, with a comma between it and the previous server. |

   A complete file, with one existing setting kept:

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

3. **Set your path.** Replace `C:/Users/you/MCP-REG-SERVER` with your folder. Use **forward
   slashes** (`/`), even on Windows, because a single backslash is invalid in JSON. Keep
   `/docker-compose.yml` at the end. macOS example: `/Users/you/MCP-REG-SERVER/docker-compose.yml`.

4. **Restart fully.** Save the file and quit Claude Desktop completely (Windows: system tray icon
   → **Quit**; macOS: **Cmd+Q**). Closing the window is not enough. Then reopen it.

5. **Check.** **Settings → Developer** lists `rag-docs` without an error, and its tools appear in a
   new chat's tools menu. A config error usually means invalid JSON, most often a missing or
   extra comma.

## Alternative: Claude Code from any folder

The `.mcp.json` registers the server only inside the cloned folder. To make it available in every
Claude Code session, register it once **from inside the cloned folder** (the command fills in the
absolute path):

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

> **`claude: command not found`?** The Claude Code CLI isn't installed or isn't on your `PATH`.
> Install it with `npm install -g @anthropic-ai/claude-code`, or open the cloned folder in the Claude desktop app's **Code** tab.
