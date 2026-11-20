BEGIN;

CREATE TABLE IF NOT EXISTS document_pages (
    page_id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL,
    document_id uuid NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
    page_number integer NOT NULL CHECK (page_number >= 1),
    native_text_coverage double precision NOT NULL CHECK (native_text_coverage BETWEEN 0 AND 1),
    ocr_used boolean NOT NULL DEFAULT false,
    ocr_confidence double precision CHECK (ocr_confidence BETWEEN 0 AND 1),
    image_artifact_id uuid,
    warnings jsonb NOT NULL DEFAULT '[]'::jsonb,
    quality_metrics jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (document_id, page_number)
);

CREATE INDEX IF NOT EXISTS document_pages_tenant_document_idx
    ON document_pages (tenant_id, document_id, page_number);

CREATE TABLE IF NOT EXISTS content_blocks (
    block_id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL,
    document_id uuid NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
    source_sha256 text NOT NULL CHECK (source_sha256 ~ '^[0-9a-f]{64}$'),
    ordinal integer NOT NULL CHECK (ordinal >= 0),
    block_type text NOT NULL,
    text text NOT NULL CHECK (length(text) > 0),
    language text NOT NULL,
    confidence double precision NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    locator jsonb NOT NULL,
    parent_block_id uuid REFERENCES content_blocks(block_id) ON DELETE SET NULL,
    text_sha256 text NOT NULL CHECK (text_sha256 ~ '^[0-9a-f]{64}$'),
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    payload_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    queryable_evidence boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (document_id, ordinal)
);

CREATE INDEX IF NOT EXISTS content_blocks_tenant_document_ordinal_idx
    ON content_blocks (tenant_id, document_id, ordinal);
CREATE INDEX IF NOT EXISTS content_blocks_tenant_document_type_idx
    ON content_blocks (tenant_id, document_id, block_type);

CREATE TABLE IF NOT EXISTS evidence_assets (
    artifact_id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL,
    document_id uuid NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
    page_number integer NOT NULL CHECK (page_number >= 1),
    tier text NOT NULL CHECK (tier IN ('low', 'high', 'original', 'derived')),
    mime_type text NOT NULL,
    object_key text NOT NULL,
    sha256 text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    byte_size bigint NOT NULL CHECK (byte_size >= 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (document_id, object_key)
);

CREATE INDEX IF NOT EXISTS evidence_assets_tenant_document_page_idx
    ON evidence_assets (tenant_id, document_id, page_number, tier);

COMMIT;
