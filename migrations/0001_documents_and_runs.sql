BEGIN;

CREATE TABLE IF NOT EXISTS documents (
    document_id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL,
    matter_id uuid,
    source_sha256 text NOT NULL CHECK (source_sha256 ~ '^[0-9a-f]{64}$'),
    original_filename text NOT NULL,
    detected_mime text NOT NULL,
    byte_size bigint NOT NULL CHECK (byte_size >= 0),
    schema_version text NOT NULL,
    extractor_version text NOT NULL,
    parser_config_hash text NOT NULL CHECK (parser_config_hash ~ '^[0-9a-f]{64}$'),
    quality jsonb NOT NULL DEFAULT '{}'::jsonb,
    status text NOT NULL CHECK (status IN (
        'received', 'validating', 'quarantined', 'extracting', 'needs_ocr',
        'structuring', 'indexing', 'ready', 'partial', 'failed', 'deleted'
    )),
    legal_hold boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    deleted_at timestamptz
);

CREATE UNIQUE INDEX IF NOT EXISTS documents_tenant_source_version_uq
    ON documents (tenant_id, source_sha256, extractor_version, parser_config_hash);
CREATE INDEX IF NOT EXISTS documents_tenant_status_idx
    ON documents (tenant_id, status, updated_at DESC);
CREATE INDEX IF NOT EXISTS documents_tenant_matter_idx
    ON documents (tenant_id, matter_id);

CREATE TABLE IF NOT EXISTS ingestion_runs (
    run_id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL,
    document_id uuid NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
    status text NOT NULL CHECK (status IN (
        'received', 'validating', 'quarantined', 'extracting', 'needs_ocr',
        'structuring', 'indexing', 'ready', 'partial', 'failed', 'deleted'
    )),
    extractor_version text NOT NULL,
    parser_config_hash text NOT NULL CHECK (parser_config_hash ~ '^[0-9a-f]{64}$'),
    warnings jsonb NOT NULL DEFAULT '[]'::jsonb,
    failure jsonb,
    started_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ingestion_runs_tenant_document_idx
    ON ingestion_runs (tenant_id, document_id, created_at DESC);

COMMIT;
