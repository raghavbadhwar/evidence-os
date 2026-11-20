BEGIN;

CREATE TABLE IF NOT EXISTS document_acl (
    acl_id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL,
    document_id uuid NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
    user_id uuid NOT NULL,
    matter_id uuid,
    access_role text NOT NULL CHECK (access_role IN ('owner', 'reviewer', 'reader', 'service_worker')),
    granted_at timestamptz NOT NULL DEFAULT now(),
    revoked_at timestamptz,
    UNIQUE (document_id, user_id)
);

CREATE INDEX IF NOT EXISTS document_acl_tenant_user_active_idx
    ON document_acl (tenant_id, user_id, revoked_at);
CREATE INDEX IF NOT EXISTS document_acl_tenant_document_active_idx
    ON document_acl (tenant_id, document_id, revoked_at);

CREATE TABLE IF NOT EXISTS retention_policies (
    policy_id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL,
    matter_id uuid,
    retention_days integer NOT NULL CHECK (retention_days > 0),
    legal_hold_default boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, matter_id)
);

CREATE TABLE IF NOT EXISTS outbox_events (
    event_id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL,
    aggregate_type text NOT NULL,
    aggregate_id uuid NOT NULL,
    event_type text NOT NULL,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    available_at timestamptz NOT NULL DEFAULT now(),
    claimed_at timestamptz,
    completed_at timestamptz,
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    last_error text,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS outbox_events_claim_idx
    ON outbox_events (available_at, completed_at, claimed_at);

CREATE TABLE IF NOT EXISTS jobs (
    job_id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL,
    kind text NOT NULL,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    idempotency_key text NOT NULL,
    status text NOT NULL CHECK (status IN ('pending', 'running', 'completed', 'dead_letter', 'cancelled')),
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    available_at timestamptz NOT NULL DEFAULT now(),
    claimed_by text,
    lease_until timestamptz,
    last_error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS jobs_claim_idx
    ON jobs (tenant_id, status, available_at, lease_until);
CREATE UNIQUE INDEX IF NOT EXISTS jobs_tenant_idempotency_uq
    ON jobs (tenant_id, idempotency_key);

CREATE TABLE IF NOT EXISTS audit_events (
    audit_id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL,
    actor_id uuid,
    actor_role text,
    action_type text NOT NULL,
    aggregate_type text NOT NULL,
    aggregate_id uuid NOT NULL,
    payload_sha256 text NOT NULL CHECK (payload_sha256 ~ '^[0-9a-f]{64}$'),
    previous_hash text,
    event_hash text NOT NULL CHECK (event_hash ~ '^[0-9a-f]{64}$'),
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS audit_events_tenant_aggregate_created_idx
    ON audit_events (tenant_id, aggregate_type, aggregate_id, created_at DESC);

COMMIT;
