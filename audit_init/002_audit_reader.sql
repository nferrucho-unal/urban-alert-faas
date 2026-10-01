DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'audit_reader') THEN
        CREATE ROLE audit_reader LOGIN PASSWORD 'audit_reader_secure_pass';
    END IF;
END $$;

GRANT CONNECT ON DATABASE urban_alert_audit TO audit_reader;
GRANT USAGE ON SCHEMA public TO audit_reader;
GRANT SELECT ON audit_events, audit_chain_state TO audit_reader;
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON audit_events, audit_chain_state FROM audit_reader;
