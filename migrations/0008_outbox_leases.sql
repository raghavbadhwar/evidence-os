BEGIN;

ALTER TABLE outbox_events
    ADD COLUMN IF NOT EXISTS idempotency_key text,
    ADD COLUMN IF NOT EXISTS status text NOT NULL DEFAULT 'pending',
    ADD COLUMN IF NOT EXISTS claimed_by text,
    ADD COLUMN IF NOT EXISTS lease_until timestamptz,
    ADD COLUMN IF NOT EXISTS updated_at timestamptz NOT NULL DEFAULT now();

ALTER TABLE outbox_events
    DROP CONSTRAINT IF EXISTS outbox_events_status_check;
ALTER TABLE outbox_events
    ADD CONSTRAINT outbox_events_status_check
    CHECK (status IN ('pending', 'running', 'completed', 'dead_letter'));

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'outbox_events_tenant_idempotency_uq'
    ) THEN
        ALTER TABLE outbox_events
            ADD CONSTRAINT outbox_events_tenant_idempotency_uq
            UNIQUE (tenant_id, idempotency_key);
    END IF;
END
$$;
CREATE INDEX IF NOT EXISTS outbox_events_lease_claim_idx
    ON outbox_events (tenant_id, status, available_at, lease_until);

COMMIT;
