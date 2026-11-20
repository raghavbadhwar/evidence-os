BEGIN;

-- RLS filters rows, but composite foreign keys also make tenant/document and
-- tenant/block relationships impossible to encode inconsistently.
CREATE UNIQUE INDEX IF NOT EXISTS documents_tenant_document_uq
    ON documents (tenant_id, document_id);
CREATE UNIQUE INDEX IF NOT EXISTS content_blocks_tenant_document_block_uq
    ON content_blocks (tenant_id, document_id, block_id);
CREATE UNIQUE INDEX IF NOT EXISTS structure_nodes_tenant_document_node_uq
    ON structure_nodes (tenant_id, document_id, node_id);

DO $$
DECLARE
    constraint_name text;
BEGIN
    FOR constraint_name IN
        SELECT c.conname
        FROM pg_constraint c
        WHERE c.conrelid = 'content_blocks'::regclass
          AND c.confrelid = 'content_blocks'::regclass
          AND c.contype = 'f'
    LOOP
        EXECUTE format('ALTER TABLE content_blocks DROP CONSTRAINT %I', constraint_name);
    END LOOP;

    FOR constraint_name IN
        SELECT c.conname
        FROM pg_constraint c
        WHERE c.conrelid = 'structure_nodes'::regclass
          AND c.confrelid = 'content_blocks'::regclass
          AND c.contype = 'f'
    LOOP
        EXECUTE format('ALTER TABLE structure_nodes DROP CONSTRAINT %I', constraint_name);
    END LOOP;

    FOR constraint_name IN
        SELECT c.conname
        FROM pg_constraint c
        WHERE c.conrelid = 'structure_nodes'::regclass
          AND c.confrelid = 'structure_nodes'::regclass
          AND c.contype = 'f'
    LOOP
        EXECUTE format('ALTER TABLE structure_nodes DROP CONSTRAINT %I', constraint_name);
    END LOOP;

    FOR constraint_name IN
        SELECT c.conname
        FROM pg_constraint c
        WHERE c.conrelid = 'verified_citations'::regclass
          AND c.confrelid = 'content_blocks'::regclass
          AND c.contype = 'f'
    LOOP
        EXECUTE format('ALTER TABLE verified_citations DROP CONSTRAINT %I', constraint_name);
    END LOOP;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'content_blocks_tenant_document_fk') THEN
        ALTER TABLE content_blocks
            ADD CONSTRAINT content_blocks_tenant_document_fk
            FOREIGN KEY (tenant_id, document_id)
            REFERENCES documents (tenant_id, document_id)
            ON DELETE CASCADE;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'content_blocks_parent_tenant_fk') THEN
        ALTER TABLE content_blocks
            ADD CONSTRAINT content_blocks_parent_tenant_fk
            FOREIGN KEY (tenant_id, document_id, parent_block_id)
            REFERENCES content_blocks (tenant_id, document_id, block_id)
            ON DELETE CASCADE;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'document_pages_tenant_document_fk') THEN
        ALTER TABLE document_pages
            ADD CONSTRAINT document_pages_tenant_document_fk
            FOREIGN KEY (tenant_id, document_id)
            REFERENCES documents (tenant_id, document_id)
            ON DELETE CASCADE;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'evidence_assets_tenant_document_fk') THEN
        ALTER TABLE evidence_assets
            ADD CONSTRAINT evidence_assets_tenant_document_fk
            FOREIGN KEY (tenant_id, document_id)
            REFERENCES documents (tenant_id, document_id)
            ON DELETE CASCADE;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ingestion_runs_tenant_document_fk') THEN
        ALTER TABLE ingestion_runs
            ADD CONSTRAINT ingestion_runs_tenant_document_fk
            FOREIGN KEY (tenant_id, document_id)
            REFERENCES documents (tenant_id, document_id)
            ON DELETE CASCADE;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'structure_nodes_tenant_document_fk') THEN
        ALTER TABLE structure_nodes
            ADD CONSTRAINT structure_nodes_tenant_document_fk
            FOREIGN KEY (tenant_id, document_id)
            REFERENCES documents (tenant_id, document_id)
            ON DELETE CASCADE;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'structure_nodes_start_block_tenant_fk') THEN
        ALTER TABLE structure_nodes
            ADD CONSTRAINT structure_nodes_start_block_tenant_fk
            FOREIGN KEY (tenant_id, document_id, start_block_id)
            REFERENCES content_blocks (tenant_id, document_id, block_id)
            ON DELETE CASCADE;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'structure_nodes_end_block_tenant_fk') THEN
        ALTER TABLE structure_nodes
            ADD CONSTRAINT structure_nodes_end_block_tenant_fk
            FOREIGN KEY (tenant_id, document_id, end_block_id)
            REFERENCES content_blocks (tenant_id, document_id, block_id)
            ON DELETE CASCADE;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'structure_nodes_parent_tenant_fk') THEN
        ALTER TABLE structure_nodes
            ADD CONSTRAINT structure_nodes_parent_tenant_fk
            FOREIGN KEY (tenant_id, document_id, parent_node_id)
            REFERENCES structure_nodes (tenant_id, document_id, node_id)
            ON DELETE CASCADE;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'index_versions_tenant_document_fk') THEN
        ALTER TABLE index_versions
            ADD CONSTRAINT index_versions_tenant_document_fk
            FOREIGN KEY (tenant_id, document_id)
            REFERENCES documents (tenant_id, document_id)
            ON DELETE CASCADE;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'retrieval_events_tenant_document_fk') THEN
        ALTER TABLE retrieval_events
            ADD CONSTRAINT retrieval_events_tenant_document_fk
            FOREIGN KEY (tenant_id, document_id)
            REFERENCES documents (tenant_id, document_id)
            ON DELETE CASCADE;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'verified_citations_tenant_document_fk') THEN
        ALTER TABLE verified_citations
            ADD CONSTRAINT verified_citations_tenant_document_fk
            FOREIGN KEY (tenant_id, document_id)
            REFERENCES documents (tenant_id, document_id)
            ON DELETE CASCADE;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'verified_citations_block_tenant_fk') THEN
        ALTER TABLE verified_citations
            ADD CONSTRAINT verified_citations_block_tenant_fk
            FOREIGN KEY (tenant_id, document_id, block_id)
            REFERENCES content_blocks (tenant_id, document_id, block_id)
            ON DELETE RESTRICT;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'document_acl_tenant_document_fk') THEN
        ALTER TABLE document_acl
            ADD CONSTRAINT document_acl_tenant_document_fk
            FOREIGN KEY (tenant_id, document_id)
            REFERENCES documents (tenant_id, document_id)
            ON DELETE CASCADE;
    END IF;
END
$$;

COMMIT;
