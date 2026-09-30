CREATE TABLE IF NOT EXISTS reportes_geo (
    reporte_id UUID PRIMARY KEY,
    municipio_id UUID NOT NULL,
    categoria TEXT NOT NULL,
    estado VARCHAR(30) NOT NULL,
    geom GEOMETRY(Point, 4326) NOT NULL,
    creado_en TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_reportes_geo_gist
    ON reportes_geo USING GIST (geom);
CREATE INDEX IF NOT EXISTS ix_reportes_geo_municipio_estado
    ON reportes_geo (municipio_id, estado);

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'svc_geo_projector') THEN
        CREATE ROLE svc_geo_projector LOGIN PASSWORD 'geo_projector_secure_pass';
    END IF;
END $$;

GRANT CONNECT ON DATABASE urban_alert_geo TO svc_geo_projector;
GRANT USAGE ON SCHEMA public TO svc_geo_projector;
GRANT SELECT, INSERT, UPDATE ON reportes_geo TO svc_geo_projector;
GRANT SELECT ON reportes_geo TO analista_obras;