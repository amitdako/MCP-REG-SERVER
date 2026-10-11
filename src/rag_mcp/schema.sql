-- Applied idempotently on every server start (see db.ensure_schema).
-- __EMBEDDING_DIM__ is replaced with Settings.embedding_dim.

CREATE EXTENSION IF NOT EXISTS vector;

-- One row per ingested document (a file, a URL, or a piece of raw text).
CREATE TABLE IF NOT EXISTS sources (
    id            BIGSERIAL PRIMARY KEY,
    source_uri    TEXT        NOT NULL UNIQUE,
    source_type   TEXT        NOT NULL CHECK (source_type IN ('file', 'url', 'text')),
    title         TEXT,
    -- sha256(content + chunker version + model name): unchanged => skip re-embedding.
    content_hash  TEXT        NOT NULL,
    chunk_count   INTEGER     NOT NULL DEFAULT 0,
    total_tokens  INTEGER     NOT NULL DEFAULT 0,
    metadata      JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS document_chunks (
    id               BIGSERIAL PRIMARY KEY,
    source_id        BIGINT      NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    chunk_index      INTEGER     NOT NULL,
    -- e.g. {"Aiven for Apache Kafka advanced parameters", "kafka"}
    heading_path     TEXT[]      NOT NULL DEFAULT '{}',
    -- Clean text returned to Claude.
    content          TEXT        NOT NULL,
    -- Breadcrumb + content: what is actually embedded and keyword-indexed.
    embedding_input  TEXT        NOT NULL,
    token_count      INTEGER     NOT NULL,
    metadata         JSONB       NOT NULL DEFAULT '{}'::jsonb,
    embedding        vector(__EMBEDDING_DIM__) NOT NULL,
    -- 'simple' = no stemming/stopwords, so identifiers like max_connections match exactly.
    content_tsv      tsvector GENERATED ALWAYS AS (to_tsvector('simple', embedding_input)) STORED,
    UNIQUE (source_id, chunk_index)
);

-- Fast CASCADE deletes and per-source lookups.
CREATE INDEX IF NOT EXISTS idx_document_chunks_source_id
    ON document_chunks (source_id);

-- Approximate nearest-neighbour search (cosine). HNSW needs no training step, unlike IVFFlat.
CREATE INDEX IF NOT EXISTS idx_document_chunks_embedding
    ON document_chunks USING hnsw (embedding vector_cosine_ops);

-- Keyword side of hybrid search.
CREATE INDEX IF NOT EXISTS idx_document_chunks_tsv
    ON document_chunks USING gin (content_tsv);
