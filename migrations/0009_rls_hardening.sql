BEGIN;

CREATE UNIQUE INDEX IF NOT EXISTS outbox_events_tenant_idempotency_uq
    ON outbox_events (tenant_id, idempotency_key);

ALTER POLICY documents_acl_policy ON documents
    USING (
        (
            app.is_service_worker()
            AND app.in_current_tenant(tenant_id)
        )
        OR (
            app.in_current_tenant(tenant_id)
            AND status <> 'deleted'
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
    WITH CHECK (app.is_service_worker() AND app.in_current_tenant(tenant_id));

ALTER POLICY document_acl_policy ON document_acl
    USING (
        (app.is_service_worker() AND app.in_current_tenant(tenant_id))
        OR (
            app.in_current_tenant(tenant_id)
            AND user_id = app.current_user_id()
            AND revoked_at IS NULL
            AND (matter_id IS NULL OR matter_id = app.current_matter_id())
        )
    )
    WITH CHECK (app.is_service_worker() AND app.in_current_tenant(tenant_id));

ALTER POLICY retention_policy_tenant ON retention_policies
    USING (
        app.in_current_tenant(tenant_id)
        AND (
            app.is_service_worker()
            OR matter_id IS NULL
            OR matter_id = app.current_matter_id()
        )
    )
    WITH CHECK (app.is_service_worker() AND app.in_current_tenant(tenant_id));

ALTER POLICY tenant_document_policy ON ingestion_runs
    USING (app.in_current_tenant(tenant_id) AND (app.is_service_worker() OR EXISTS (
        SELECT 1 FROM documents d
        WHERE d.document_id = ingestion_runs.document_id
          AND d.tenant_id = ingestion_runs.tenant_id
          AND d.status <> 'deleted'
    )))
    WITH CHECK (app.is_service_worker() AND app.in_current_tenant(tenant_id));

ALTER POLICY tenant_document_policy ON document_pages
    USING (app.in_current_tenant(tenant_id) AND (app.is_service_worker() OR EXISTS (
        SELECT 1 FROM documents d
        WHERE d.document_id = document_pages.document_id
          AND d.tenant_id = document_pages.tenant_id
          AND d.status <> 'deleted'
    )))
    WITH CHECK (app.is_service_worker() AND app.in_current_tenant(tenant_id));

ALTER POLICY tenant_document_policy ON content_blocks
    USING (app.in_current_tenant(tenant_id) AND (app.is_service_worker() OR EXISTS (
        SELECT 1 FROM documents d
        WHERE d.document_id = content_blocks.document_id
          AND d.tenant_id = content_blocks.tenant_id
          AND d.status <> 'deleted'
    )))
    WITH CHECK (app.is_service_worker() AND app.in_current_tenant(tenant_id));

ALTER POLICY tenant_document_policy ON evidence_assets
    USING (app.in_current_tenant(tenant_id) AND (app.is_service_worker() OR EXISTS (
        SELECT 1 FROM documents d
        WHERE d.document_id = evidence_assets.document_id
          AND d.tenant_id = evidence_assets.tenant_id
          AND d.status <> 'deleted'
    )))
    WITH CHECK (app.is_service_worker() AND app.in_current_tenant(tenant_id));

ALTER POLICY tenant_document_policy ON structure_nodes
    USING (app.in_current_tenant(tenant_id) AND (app.is_service_worker() OR EXISTS (
        SELECT 1 FROM documents d
        WHERE d.document_id = structure_nodes.document_id
          AND d.tenant_id = structure_nodes.tenant_id
          AND d.status <> 'deleted'
    )))
    WITH CHECK (app.is_service_worker() AND app.in_current_tenant(tenant_id));

ALTER POLICY tenant_document_policy ON index_versions
    USING (app.in_current_tenant(tenant_id) AND (app.is_service_worker() OR EXISTS (
        SELECT 1 FROM documents d
        WHERE d.document_id = index_versions.document_id
          AND d.tenant_id = index_versions.tenant_id
          AND d.status <> 'deleted'
    )))
    WITH CHECK (app.is_service_worker() AND app.in_current_tenant(tenant_id));

ALTER POLICY tenant_document_policy ON retrieval_events
    USING (app.in_current_tenant(tenant_id) AND (app.is_service_worker() OR EXISTS (
        SELECT 1 FROM documents d
        WHERE d.document_id = retrieval_events.document_id
          AND d.tenant_id = retrieval_events.tenant_id
          AND d.status <> 'deleted'
    )))
    WITH CHECK (app.is_service_worker() AND app.in_current_tenant(tenant_id));

ALTER POLICY tenant_document_policy ON verified_citations
    USING (app.in_current_tenant(tenant_id) AND (app.is_service_worker() OR EXISTS (
        SELECT 1 FROM documents d
        WHERE d.document_id = verified_citations.document_id
          AND d.tenant_id = verified_citations.tenant_id
          AND d.status <> 'deleted'
    )))
    WITH CHECK (app.is_service_worker() AND app.in_current_tenant(tenant_id));

COMMIT;
