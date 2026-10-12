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

### Example 1: a technical parameter

- **Question:** *"What is the default tcp_keepalives_idle for Aiven for PostgreSQL?"*
- **Retrieved context:** [`idle-connections.rst`](https://github.com/aiven/devportal/blob/main/docs/products/postgresql/reference/idle-connections.rst),
  section *Keep-alive server side parameters* (vector rank #1, keyword rank #1)
- **Answer:** `180` seconds.

### Example 2: a calculation from a rule

- **Question:** *"How many static IP addresses do I need for a 9 node plan?"*
- **Retrieved context:** [`static-ip-addresses.rst`](https://github.com/aiven/devportal/blob/main/docs/platform/howto/static-ip-addresses.rst),
  section *Calculate the number of IP addresses needed* (vector rank #1, keyword rank #1)
- **Answer:** `15` static IPs (9 nodes + 6, the rule for plans above 6 nodes).

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

### Questions to try

Each of these was checked against the ingested corpus; the first result comes from the page listed:

| Question | Expected source |
|---|---|
| How do I enable automatic topic creation in Kafka? | `create-topics-automatically.rst` |
| What does auto_create_topics_enable do? | `create-topics-automatically.rst` (the parameter is also in `kafka/reference/advanced-params.rst`) |
| How often does Aiven for ClickHouse take backups? | `platform/concepts/service_backups.rst` (daily, retention by plan) |
| How many connections per GiB of RAM does Aiven for MySQL allow? | `mysql/concepts/max-number-of-connections.rst` |
| What minimum monthly spend is required for Bring Your Own Cloud? | `platform/concepts/byoc.rst` |

## Ingesting content

`ingest(text | path | url, name?)` stores content for search. Pass exactly one of `text`, `path`
or `url`.

| Input | Example | Details |
|---|---|---|
| `text` | `ingest(text="# Team notes ...", name="team-notes")` | Raw text or Markdown. `name` gives it a stable URI (`text:team-notes`), so ingesting again under the same name replaces it. |
| `path` | `ingest(path="/data/notes.md")`, `ingest(path="/data")` | A file or folder inside the container. The host's `./data` folder is mounted read-only at `/data`. Files: `.md` `.markdown` `.mdx` `.rst` `.txt`; folders: Markdown and RST only. Max 5 MB per document. |
| `url` | `ingest(url="https://github.com/aiven/devportal")` | A web page (converted to Markdown), a raw `.md`/`.rst`/`.txt` URL, a GitHub file (`/blob/...`), or a GitHub repo or folder (`/tree/main/docs/...`). |

A single document is ingested within the call:

```
created: text:team-notes (1 chunks, 15 tokens)
```

A folder or GitHub repo runs as a **background job**, and the call returns immediately:

```
Started background ingest job f15dffba for directory /data/demo. Documents become searchable as they are processed. Check progress with ingest_status(job_id="f15dffba").
```

`ingest_status(job_id?)` reports a job's progress (omit `job_id` to list all jobs of the session):

```json
{
  "job_id": "f15dffba",
  "input": "directory /data/demo",
  "state": "done",
  "files_total": 1,
  "files_processed": 1,
  "created": 1,
  "updated": 0,
  "unchanged": 0,
  "failed": 0,
  "deleted_missing": 0,
  "chunks_written": 1,
  "elapsed_s": 0
}
```

While a job runs it also reports `eta_s`; failures are listed in `errors` (first 20). Jobs live in
the server process: if the Claude session ends mid-job, run the same ingest again and the finished
documents are skipped.

**Re-ingesting is safe and cheap.** Every document is hashed (content + chunker version +
embedding model):

- **Unchanged** documents (same hash) are skipped without re-embedding:
  `unchanged: text:team-notes (1 chunks, 15 tokens)`. Re-ingesting all of devportal takes 12 s.
- **Changed** documents are replaced atomically (old chunks out, new chunks in, one transaction),
  and re-ingesting a folder or repo deletes sources whose files were removed upstream (only after
  a run with no failures).

Without Claude, `docker compose run --rm mcp-server ingest /data/notes.md` (or a URL) runs the same
pipeline in the terminal.

## Managing sources

`list_sources(filter?, limit=50, offset=0)` shows what is ingested. Every response starts with the
global totals (sources, chunks, tokens, count per source type) and any running ingest jobs, then
one page of sources, newest first:

```
Total: 816 sources, 2909 chunks, 761,942 tokens (815 file, 1 text).

Showing 2 of 2 sources matching 'notes' (offset 0):
- Release notes | /data/demo/release-notes.md | 1 chunks | updated 2026-10-12 09:26
- Team notes | text:team-notes | 1 chunks | updated 2026-10-12 09:26
```

| Parameter | Default | Meaning |
|---|---|---|
| `filter` | none | Keep only sources whose URI or title contains this text (case-insensitive). |
| `limit` | `50` | Rows per page (max 200). Pagination keeps responses small: the full devportal corpus is 814 sources. |
| `offset` | `0` | Rows to skip. When more rows exist, the response ends with `... use offset=N for more.` |

`delete_source(source_uri)` removes one source immediately: its row and, through `ON DELETE
CASCADE`, all of its chunks. Each chunk row holds both the embedding and the keyword index entry,
so the source disappears from both sides of search at once. The URI must match exactly as shown by
`list_sources` or in search results; there are no wildcards, so nothing is deleted by accident.

```
> delete_source("text:team-notes")
deleted text:team-notes (1 chunks)

> delete_source("text:does-not-exist")
Error executing tool delete_source: no source 'text:does-not-exist'; use list_sources(filter=...) to find the exact URI
```

To remove everything at once, `docker compose down -v` deletes the database volume.

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
