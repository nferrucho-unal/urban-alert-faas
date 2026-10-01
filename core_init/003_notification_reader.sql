DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'notification_reader') THEN
        CREATE ROLE notification_reader LOGIN PASSWORD 'notification_reader_secure_pass';
    END IF;
END $$;

GRANT CONNECT ON DATABASE urban_alert_core TO notification_reader;
GRANT USAGE ON SCHEMA public TO notification_reader;
GRANT SELECT (report_id, actor_id) ON reportes_core TO notification_reader;
GRANT SELECT (cognito_sub, email, display_name, activo) ON usuarios TO notification_reader;
