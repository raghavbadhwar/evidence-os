BEGIN;

CREATE SCHEMA IF NOT EXISTS app;

CREATE OR REPLACE FUNCTION app.current_tenant_id()
RETURNS uuid
LANGUAGE sql
STABLE
AS $$
    SELECT NULLIF(current_setting('app.tenant_id', true), '')::uuid
$$;

CREATE OR REPLACE FUNCTION app.current_user_id()
RETURNS uuid
LANGUAGE sql
STABLE
AS $$
    SELECT NULLIF(current_setting('app.user_id', true), '')::uuid
$$;

CREATE OR REPLACE FUNCTION app.current_matter_id()
RETURNS uuid
LANGUAGE sql
STABLE
AS $$
    SELECT NULLIF(current_setting('app.matter_id', true), '')::uuid
$$;

CREATE OR REPLACE FUNCTION app.is_service_worker()
RETURNS boolean
LANGUAGE sql
STABLE
AS $$
    SELECT current_setting('app.actor_role', true) IN ('service_worker', 'admin')
$$;

CREATE OR REPLACE FUNCTION app.in_current_tenant(candidate uuid)
RETURNS boolean
LANGUAGE sql
STABLE
AS $$
    SELECT candidate = app.current_tenant_id()
$$;

ALTER TABLE documents ENABLE ROW LEVEL SECURITY;
ALTER TABLE ingestion_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE document_pages ENABLE ROW LEVEL SECURITY;
ALTER TABLE content_blocks ENABLE ROW LEVEL SECURITY;
ALTER TABLE evidence_assets ENABLE ROW LEVEL SECURITY;
ALTER TABLE structure_nodes ENABLE ROW LEVEL SECURITY;
ALTER TABLE index_versions ENABLE ROW LEVEL SECURITY;
ALTER TABLE retrieval_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE verified_citations ENABLE ROW LEVEL SECURITY;
ALTER TABLE document_acl ENABLE ROW LEVEL SECURITY;
ALTER TABLE retention_policies ENABLE ROW LEVEL SECURITY;
ALTER TABLE outbox_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE jobs ENABLE ROW LEVEL SECURITY;

ALTER TABLE documents FORCE ROW LEVEL SECURITY;
ALTER TABLE ingestion_runs FORCE ROW LEVEL SECURITY;
ALTER TABLE document_pages FORCE ROW LEVEL SECURITY;
ALTER TABLE content_blocks FORCE ROW LEVEL SECURITY;
ALTER TABLE evidence_assets FORCE ROW LEVEL SECURITY;
ALTER TABLE structure_nodes FORCE ROW LEVEL SECURITY;
ALTER TABLE index_versions FORCE ROW LEVEL SECURITY;
ALTER TABLE retrieval_events FORCE ROW LEVEL SECURITY;
ALTER TABLE verified_citations FORCE ROW LEVEL SECURITY;
ALTER TABLE document_acl FORCE ROW LEVEL SECURITY;
ALTER TABLE retention_policies FORCE ROW LEVEL SECURITY;
ALTER TABLE outbox_events FORCE ROW LEVEL SECURITY;
ALTER TABLE audit_events FORCE ROW LEVEL SECURITY;
ALTER TABLE jobs FORCE ROW LEVEL SECURITY;

CREATE POLICY documents_acl_policy ON documents
    USING (
        (
            app.is_service_worker()
            AND app.in_current_tenant(tenant_id)
        )
        OR (
            app.in_current_tenant(tenant_id)
            AND EXISTS (
                SELECT 1 FROM document_acl acl
                WHERE acl.document_id = documents.document_id
                  AND acl.tenant_id = documents.tenant_id
                  AND acl.user_id = app.current_user_id()
                  AND acl.revoked_at IS NULL
                  AND (documents.matter_id IS NULL OR documents.matter_id = app.current_matter_id())
            )
        )
    )
    WITH CHECK (app.in_current_tenant(tenant_id));

CREATE POLICY document_acl_policy ON document_acl
    USING (
        (app.is_service_worker() AND app.in_current_tenant(tenant_id))
        OR (
            app.in_current_tenant(tenant_id)
            AND user_id = app.current_user_id()
            AND (matter_id IS NULL OR matter_id = app.current_matter_id())
        )
    )
    WITH CHECK (
        (app.is_service_worker() AND app.in_current_tenant(tenant_id))
        OR (
            app.in_current_tenant(tenant_id)
            AND user_id = app.current_user_id()
            AND (matter_id IS NULL OR matter_id = app.current_matter_id())
        )
    );

CREATE POLICY retention_policy_tenant ON retention_policies
    USING (app.in_current_tenant(tenant_id))
    WITH CHECK (app.in_current_tenant(tenant_id));

CREATE POLICY outbox_service_policy ON outbox_events
    USING (app.is_service_worker() AND app.in_current_tenant(tenant_id))
    WITH CHECK (app.is_service_worker() AND app.in_current_tenant(tenant_id));

CREATE POLICY jobs_service_policy ON jobs
    USING (app.is_service_worker() AND app.in_current_tenant(tenant_id))
    WITH CHECK (app.is_service_worker() AND app.in_current_tenant(tenant_id));

CREATE POLICY audit_tenant_policy ON audit_events
    USING (app.in_current_tenant(tenant_id))
    WITH CHECK (app.is_service_worker() AND app.in_current_tenant(tenant_id));

CREATE POLICY tenant_document_policy ON ingestion_runs
    USING (app.in_current_tenant(tenant_id) AND (app.is_service_worker() OR EXISTS (
        SELECT 1 FROM documents d
        WHERE d.document_id = ingestion_runs.document_id AND d.tenant_id = ingestion_runs.tenant_id
    )))
    WITH CHECK (app.in_current_tenant(tenant_id) AND (app.is_service_worker() OR EXISTS (
        SELECT 1 FROM documents d
        WHERE d.document_id = ingestion_runs.document_id AND d.tenant_id = ingestion_runs.tenant_id
    )));

CREATE POLICY tenant_document_policy ON document_pages
    USING (app.in_current_tenant(tenant_id) AND (app.is_service_worker() OR EXISTS (
        SELECT 1 FROM documents d
        WHERE d.document_id = document_pages.document_id AND d.tenant_id = document_pages.tenant_id
    )))
    WITH CHECK (app.in_current_tenant(tenant_id) AND (app.is_service_worker() OR EXISTS (
        SELECT 1 FROM documents d
        WHERE d.document_id = document_pages.document_id AND d.tenant_id = document_pages.tenant_id
    )));

CREATE POLICY tenant_document_policy ON content_blocks
    USING (app.in_current_tenant(tenant_id) AND (app.is_service_worker() OR EXISTS (
        SELECT 1 FROM documents d
        WHERE d.document_id = content_blocks.document_id AND d.tenant_id = content_blocks.tenant_id
    )))
    WITH CHECK (app.in_current_tenant(tenant_id) AND (app.is_service_worker() OR EXISTS (
        SELECT 1 FROM documents d
        WHERE d.document_id = content_blocks.document_id AND d.tenant_id = content_blocks.tenant_id
    )));

CREATE POLICY tenant_document_policy ON evidence_assets
    USING (app.in_current_tenant(tenant_id) AND (app.is_service_worker() OR EXISTS (
        SELECT 1 FROM documents d
        WHERE d.document_id = evidence_assets.document_id AND d.tenant_id = evidence_assets.tenant_id
    )))
    WITH CHECK (app.in_current_tenant(tenant_id) AND (app.is_service_worker() OR EXISTS (
        SELECT 1 FROM documents d
        WHERE d.document_id = evidence_assets.document_id AND d.tenant_id = evidence_assets.tenant_id
    )));

CREATE POLICY tenant_document_policy ON structure_nodes
    USING (app.in_current_tenant(tenant_id) AND (app.is_service_worker() OR EXISTS (
        SELECT 1 FROM documents d
        WHERE d.document_id = structure_nodes.document_id AND d.tenant_id = structure_nodes.tenant_id
    )))
    WITH CHECK (app.in_current_tenant(tenant_id) AND (app.is_service_worker() OR EXISTS (
        SELECT 1 FROM documents d
        WHERE d.document_id = structure_nodes.document_id AND d.tenant_id = structure_nodes.tenant_id
    )));

CREATE POLICY tenant_document_policy ON index_versions
    USING (app.in_current_tenant(tenant_id) AND (app.is_service_worker() OR EXISTS (
        SELECT 1 FROM documents d
        WHERE d.document_id = index_versions.document_id AND d.tenant_id = index_versions.tenant_id
    )))
    WITH CHECK (app.in_current_tenant(tenant_id) AND (app.is_service_worker() OR EXISTS (
        SELECT 1 FROM documents d
        WHERE d.document_id = index_versions.document_id AND d.tenant_id = index_versions.tenant_id
    )));

CREATE POLICY tenant_document_policy ON retrieval_events
    USING (app.in_current_tenant(tenant_id) AND (app.is_service_worker() OR EXISTS (
        SELECT 1 FROM documents d
        WHERE d.document_id = retrieval_events.document_id AND d.tenant_id = retrieval_events.tenant_id
    )))
    WITH CHECK (app.in_current_tenant(tenant_id) AND (app.is_service_worker() OR EXISTS (
        SELECT 1 FROM documents d
        WHERE d.document_id = retrieval_events.document_id AND d.tenant_id = retrieval_events.tenant_id
    )));

CREATE POLICY tenant_document_policy ON verified_citations
    USING (app.in_current_tenant(tenant_id) AND (app.is_service_worker() OR EXISTS (
        SELECT 1 FROM documents d
        WHERE d.document_id = verified_citations.document_id AND d.tenant_id = verified_citations.tenant_id
    )))
    WITH CHECK (app.in_current_tenant(tenant_id) AND (app.is_service_worker() OR EXISTS (
        SELECT 1 FROM documents d
        WHERE d.document_id = verified_citations.document_id AND d.tenant_id = verified_citations.tenant_id
    )));

COMMIT;
