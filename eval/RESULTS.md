# Retrieval eval results

Run: 2026-10-12, chunker version 2 (list-tables as one line per row, wrapped links cleaned), corpus: aiven/devportal (814 sources, 2,914 chunks), model bge-small-en-v1.5.
Reproduce: `docker compose run --rm --entrypoint python mcp-server scripts/eval_retrieval.py`

**Method.** 30 questions in [questions.jsonl](questions.jsonl): 20 from pages sampled with `random.Random(42)` among pages with >= 150 tokens (taken in sample order; none skipped), 10 targeted at what the task says will be tested (config parameters, limits, how-to steps). Each lists every page that contains the answer. A question counts as answered at rank r if the r-th returned chunk comes from an expected page.

**Decision taken from this run:** the identifier boost matches plain RRF on 29 of 30 questions and is one rank worse on t02, so plain RRF stays (simpler, no measured benefit).

**Caveats:** n=30 (one question = 3.3%); questions were written by the system author (mitigated by random page sampling); "page found" is measured, not answer correctness.

## Results

| config | hit@1 | hit@5 | MRR@10 | avg latency |
|---|---|---|---|---|
| vector only | 70% | 90% | 0.79 | 48 ms |
| keyword only | 73% | 93% | 0.83 | 48 ms |
| hybrid (RRF) | 80% | 97% | 0.87 | 56 ms |
| hybrid + id boost x2 | 80% | 97% | 0.87 | 51 ms |

### hit@5 by slice

| slice | n | vector only | keyword only | hybrid (RRF) | hybrid + id boost x2 |
|---|---|---|---|---|---|
| category=config | 8 | 88% | 100% | 100% | 100% |
| category=fact | 6 | 100% | 100% | 100% | 100% |
| category=howto | 9 | 78% | 89% | 89% | 89% |
| category=limit | 7 | 100% | 86% | 100% | 100% |
| origin=random | 20 | 85% | 100% | 95% | 95% |
| origin=targeted | 10 | 100% | 80% | 100% | 100% |
| phrasing=exact | 8 | 100% | 100% | 100% | 100% |
| phrasing=paraphrase | 22 | 86% | 91% | 95% | 95% |

### Per question (rank of first correct chunk, - = not in top 10)

| id | question | vector only | keyword only | hybrid (RRF) | hybrid + id boost x2 |
|---|---|---|---|---|---|
| r01 | How many static IP addresses do I need for a service with a 9 node pla | 1 | 1 | 1 | 1 |
| r02 | Which port is used for Aiven platform management traffic on service no | 1 | 1 | 1 | 1 |
| r03 | What should fetch.max.wait.ms be set to on the client when using Kafka | 2 | 2 | 2 | 2 |
| r04 | How do I set up an SMTP server so Aiven for Grafana can send invite an | 1 | 1 | 1 | 1 |
| r05 | How can I generate random test data as a source table in an Apache Fli | 1 | 1 | 1 | 1 |
| r06 | What tool can I use to accurately count the records in an Aiven for Ap | - | 1 | 10 | 10 |
| r07 | Which FusionAuth API endpoint must the API key be allowed to POST to w | 1 | 1 | 1 | 1 |
| r08 | What happens if a pg_cron job is still running when its next scheduled | 1 | 1 | 1 | 1 |
| r09 | What are the steps to move a public Aiven service into a VPC without l | 1 | 1 | 1 | 1 |
| r10 | Which avn command adds a credit code to a project? | 1 | 2 | 1 | 1 |
| r11 | How do I find which MySQL tables are missing a primary key? | 1 | 1 | 1 | 1 |
| r12 | How is data at rest encrypted on the disks of Aiven service virtual ma | 1 | 1 | 1 | 1 |
| r13 | What minimum monthly cloud spend is required to be eligible for Bring  | 1 | 1 | 1 | 1 |
| r14 | Why do I get the error 'Creating plan this large requires valid paymen | 1 | 1 | 1 | 1 |
| r15 | Does a PostgreSQL table used as the sink of a Flink pipeline need to e | 1 | 1 | 1 | 1 |
| r16 | What does the number_of_task_slots parameter control in Aiven for Apac | 1 | 1 | 1 | 1 |
| r17 | Which SSL mode should I choose in pgAdmin when connecting to Aiven for | 9 | 1 | 2 | 2 |
| r18 | Which Aiven CLI command creates an integration between two services? | - | 3 | 1 | 1 |
| r19 | In which UpCloud location can Aiven automatically balance service repl | 1 | 1 | 1 | 1 |
| r20 | How many days of backup history does an Aiven for Redis Business plan  | 2 | 1 | 1 | 1 |
| t01 | How many connections per GiB of RAM does Aiven for MySQL allow on a pl | 1 | 2 | 1 | 1 |
| t02 | What is the maximum number of connections for a Startup-8 Aiven for Po | 1 | 6 | 3 | 4 |
| t03 | What does the redis_maxmemory_policy setting configure? | 2 | 1 | 1 | 1 |
| t04 | What is the default tcp_keepalives_idle for Aiven for PostgreSQL? | 1 | 1 | 1 | 1 |
| t05 | After how many seconds of inactivity does the PostgreSQL server send a | 1 | 1 | 1 | 1 |
| t06 | What does auto_create_topics_enable do in Aiven for Apache Kafka? | 1 | 1 | 1 | 1 |
| t07 | How often does Aiven for ClickHouse take backups, and is point-in-time | 4 | 1 | 1 | 1 |
| t08 | How do I enable vector similarity search with pgvector on Aiven for Po | 2 | 3 | 3 | 3 |
| t09 | How do I create a Kafka topic from the command line with the Aiven CLI | 3 | 10 | 3 | 3 |
| t10 | How much memory overhead does Aiven reserve on each service VM? | 1 | 2 | 1 | 1 |
