BEGIN;

CREATE TABLE IF NOT EXISTS structure_nodes (
    node_id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL,
    document_id uuid NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
    parent_node_id uuid REFERENCES structure_nodes(node_id) ON DELETE CASCADE,
    title text NOT NULL,
    kind text NOT NULL,
    ordinal integer NOT NULL CHECK (ordinal >= 0),
    start_block_id uuid NOT NULL REFERENCES content_blocks(block_id) ON DELETE RESTRICT,
    end_block_id uuid NOT NULL REFERENCES content_blocks(block_id) ON DELETE RESTRICT,
    method text NOT NULL,
    confidence double precision NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    verified boolean NOT NULL DEFAULT false,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    payload_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (document_id, ordinal),
    CHECK (start_block_id <> end_block_id OR start_block_id IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS structure_nodes_tenant_document_ordinal_idx
    ON structure_nodes (tenant_id, document_id, ordinal);
CREATE INDEX IF NOT EXISTS structure_nodes_tenant_document_parent_idx
    ON structure_nodes (tenant_id, document_id, parent_node_id);

CREATE TABLE IF NOT EXISTS index_versions (
    index_id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL,
    document_id uuid NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
    source_sha256 text NOT NULL CHECK (source_sha256 ~ '^[0-9a-f]{64}$'),
    index_type text NOT NULL,
    implementation text NOT NULL,
    config_hash text NOT NULL CHECK (config_hash ~ '^[0-9a-f]{64}$'),
    descriptor jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (document_id, index_type, config_hash)
);

CREATE INDEX IF NOT EXISTS index_versions_tenant_document_idx
    ON index_versions (tenant_id, document_id, created_at DESC);

CREATE INDEX IF NOT EXISTS content_blocks_simple_fts_idx
    ON content_blocks USING gin (to_tsvector('simple', text));
CREATE INDEX IF NOT EXISTS structure_nodes_simple_fts_idx
    ON structure_nodes USING gin (to_tsvector('simple', title));

COMMIT;
