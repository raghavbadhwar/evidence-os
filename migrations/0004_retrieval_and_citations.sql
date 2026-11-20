BEGIN;

CREATE TABLE IF NOT EXISTS retrieval_events (
    event_id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL,
    document_id uuid NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
    source_sha256 text NOT NULL CHECK (source_sha256 ~ '^[0-9a-f]{64}$'),
    query_sha256 text NOT NULL CHECK (query_sha256 ~ '^[0-9a-f]{64}$'),
    routes jsonb NOT NULL DEFAULT '[]'::jsonb,
    selected_block_ids uuid[] NOT NULL DEFAULT '{}'::uuid[],
    elapsed_ms double precision NOT NULL CHECK (elapsed_ms >= 0),
    input_tokens integer NOT NULL DEFAULT 0 CHECK (input_tokens >= 0),
    output_tokens integer NOT NULL DEFAULT 0 CHECK (output_tokens >= 0),
    embedding_cost numeric(18, 8) NOT NULL DEFAULT 0 CHECK (embedding_cost >= 0),
    reranker_cost numeric(18, 8) NOT NULL DEFAULT 0 CHECK (reranker_cost >= 0),
    answer_model_cost numeric(18, 8) NOT NULL DEFAULT 0 CHECK (answer_model_cost >= 0),
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS retrieval_events_tenant_document_created_idx
    ON retrieval_events (tenant_id, document_id, created_at DESC);

CREATE TABLE IF NOT EXISTS verified_citations (
    citation_id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL,
    document_id uuid NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
    source_sha256 text NOT NULL CHECK (source_sha256 ~ '^[0-9a-f]{64}$'),
    answer_span text NOT NULL,
    block_id uuid NOT NULL REFERENCES content_blocks(block_id) ON DELETE RESTRICT,
    quoted_text_sha256 text NOT NULL CHECK (quoted_text_sha256 ~ '^[0-9a-f]{64}$'),
    locator jsonb NOT NULL,
    verification_status text NOT NULL CHECK (verification_status IN ('verified', 'rejected', 'needs_review')),
    failure_reason text,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS verified_citations_tenant_document_created_idx
    ON verified_citations (tenant_id, document_id, created_at DESC);

COMMIT;
