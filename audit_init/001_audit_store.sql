CREATE TABLE IF NOT EXISTS audit_events (
    sequence_id BIGSERIAL PRIMARY KEY,
    event_id UUID NOT NULL UNIQUE,
    event_type TEXT NOT NULL,
    event_version INTEGER NOT NULL CHECK (event_version > 0),
    occurred_at TIMESTAMPTZ NOT NULL,
    correlation_id UUID NOT NULL,
    report_id UUID,
    actor_id UUID NOT NULL,
    payload JSONB NOT NULL,
    previous_hash CHAR(64) NOT NULL,
    record_hash CHAR(64) NOT NULL UNIQUE,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_audit_events_report_sequence
    ON audit_events(report_id, sequence_id);
CREATE INDEX IF NOT EXISTS ix_audit_events_correlation
    ON audit_events(correlation_id, sequence_id);

CREATE TABLE IF NOT EXISTS audit_chain_state (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    last_hash CHAR(64) NOT NULL,
    last_event_id UUID,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
INSERT INTO audit_chain_state (singleton, last_hash)
VALUES (TRUE, repeat('0', 64))
ON CONFLICT (singleton) DO NOTHING;

CREATE OR REPLACE FUNCTION reject_audit_event_mutation()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'audit_events is append-only';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_audit_events_immutable ON audit_events;
CREATE TRIGGER trg_audit_events_immutable
    BEFORE UPDATE OR DELETE ON audit_events
    FOR EACH ROW EXECUTE FUNCTION reject_audit_event_mutation();

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'audit_writer') THEN
        CREATE ROLE audit_writer LOGIN PASSWORD 'audit_writer_secure_pass';
    END IF;
END $$;

GRANT CONNECT ON DATABASE urban_alert_audit TO audit_writer;
GRANT USAGE ON SCHEMA public TO audit_writer;
GRANT SELECT, INSERT ON audit_events TO audit_writer;
GRANT SELECT ON audit_chain_state TO audit_writer;
GRANT UPDATE (last_hash, last_event_id, updated_at) ON audit_chain_state TO audit_writer;
GRANT USAGE, SELECT ON SEQUENCE audit_events_sequence_id_seq TO audit_writer;
REVOKE UPDATE, DELETE, TRUNCATE ON audit_events FROM audit_writer;
