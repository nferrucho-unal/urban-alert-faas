CREATE TABLE IF NOT EXISTS municipios (
    id UUID PRIMARY KEY,
    nombre VARCHAR(120) NOT NULL,
    activo BOOLEAN NOT NULL DEFAULT TRUE
);

INSERT INTO municipios (id, nombre)
VALUES ('22222222-2222-4222-8222-222222222222', 'Bogota')
ON CONFLICT (id) DO NOTHING;

ALTER TABLE usuarios
    ADD COLUMN IF NOT EXISTS cognito_sub UUID,
    ADD COLUMN IF NOT EXISTS email VARCHAR(254),
    ADD COLUMN IF NOT EXISTS display_name VARCHAR(120),
    ADD COLUMN IF NOT EXISTS municipio_id UUID REFERENCES municipios(id),
    ADD COLUMN IF NOT EXISTS activo BOOLEAN NOT NULL DEFAULT TRUE,
    ADD COLUMN IF NOT EXISTS creado_en TIMESTAMPTZ NOT NULL DEFAULT now(),
    ADD COLUMN IF NOT EXISTS actualizado_en TIMESTAMPTZ NOT NULL DEFAULT now();

CREATE UNIQUE INDEX IF NOT EXISTS ux_usuarios_cognito_sub
    ON usuarios(cognito_sub);

ALTER TABLE reportes_core
    ADD COLUMN IF NOT EXISTS categoria TEXT NOT NULL DEFAULT 'otro',
    ADD COLUMN IF NOT EXISTS actor_id UUID,
    ADD COLUMN IF NOT EXISTS municipio_id UUID REFERENCES municipios(id),
    ADD COLUMN IF NOT EXISTS lat DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS lon DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS correlation_id UUID,
    ADD COLUMN IF NOT EXISTS actualizado_en TIMESTAMPTZ NOT NULL DEFAULT now();

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_reportes_categoria') THEN
        ALTER TABLE reportes_core ADD CONSTRAINT chk_reportes_categoria
            CHECK (categoria IN ('hueco_via', 'fuga_agua', 'alumbrado', 'arbolado', 'otro'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_reportes_descripcion') THEN
        ALTER TABLE reportes_core ADD CONSTRAINT chk_reportes_descripcion
            CHECK (char_length(descripcion) BETWEEN 10 AND 500) NOT VALID;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_reportes_lat') THEN
        ALTER TABLE reportes_core ADD CONSTRAINT chk_reportes_lat
            CHECK (lat IS NULL OR lat BETWEEN -90 AND 90);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_reportes_lon') THEN
        ALTER TABLE reportes_core ADD CONSTRAINT chk_reportes_lon
            CHECK (lon IS NULL OR lon BETWEEN -180 AND 180);
    END IF;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS ux_reportes_correlation
    ON reportes_core(correlation_id);
CREATE INDEX IF NOT EXISTS ix_reportes_municipio_estado
    ON reportes_core(municipio_id, estado);
CREATE INDEX IF NOT EXISTS ix_reportes_actor
    ON reportes_core(actor_id);

CREATE TABLE IF NOT EXISTS reporte_historial_estado (
    id BIGSERIAL PRIMARY KEY,
    reporte_id VARCHAR(50) NOT NULL REFERENCES reportes_core(report_id),
    estado_previo VARCHAR(30),
    estado_nuevo VARCHAR(30) NOT NULL,
    actor_id UUID NOT NULL,
    motivo TEXT,
    creado_en TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS reporte_multimedia (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    reporte_id VARCHAR(50) NOT NULL REFERENCES reportes_core(report_id),
    object_key TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    tamano_bytes BIGINT,
    creado_en TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (reporte_id, object_key)
);

CREATE TABLE IF NOT EXISTS multimedia_uploads (
    upload_id UUID PRIMARY KEY,
    reporte_id VARCHAR(50) NOT NULL REFERENCES reportes_core(report_id),
    actor_id UUID NOT NULL,
    bucket TEXT NOT NULL,
    object_key TEXT NOT NULL UNIQUE,
    staging_key TEXT NOT NULL UNIQUE,
    mime_type TEXT NOT NULL,
    expected_size_bytes BIGINT NOT NULL CHECK (expected_size_bytes > 0),
    correlation_id UUID NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'PENDING'
        CHECK (status IN ('PENDING', 'CONFIRMED')),
    expires_at TIMESTAMPTZ NOT NULL,
    confirmed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_multimedia_uploads_report
    ON multimedia_uploads(reporte_id, created_at DESC);

CREATE TABLE IF NOT EXISTS outbox_eventos (
    id UUID PRIMARY KEY,
    tipo TEXT NOT NULL,
    payload JSONB NOT NULL,
    correlation_id UUID NOT NULL,
    creado_en TIMESTAMPTZ NOT NULL DEFAULT now(),
    publicado_en TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS ix_outbox_pendientes
    ON outbox_eventos(creado_en) WHERE publicado_en IS NULL;

CREATE UNIQUE INDEX IF NOT EXISTS ux_obras_report_id
    ON obras(report_id);

GRANT SELECT, INSERT, UPDATE ON municipios, usuarios, reportes_core, obras,
    reporte_historial_estado, reporte_multimedia, multimedia_uploads,
    outbox_eventos TO app_core_user;
GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO app_core_user;