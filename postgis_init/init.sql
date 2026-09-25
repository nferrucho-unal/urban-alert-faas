-- 1. Activar la extensión espacial PostGIS
CREATE EXTENSION IF NOT EXISTS postgis;

-- 2. Crear tabla para almacenar las geometrías de los reportes viales
CREATE TABLE IF NOT EXISTS reportes_geograficos (
    id SERIAL PRIMARY KEY,
    report_id VARCHAR(50) UNIQUE NOT NULL,
    tipo_daño VARCHAR(100) NOT NULL,
    estado VARCHAR(50) NOT NULL,
    -- Tipo POINT usando el sistema de coordenadas estándar WGS 84 (SRID 4326)
    ubicacion GEOMETRY(Point, 4326) NOT NULL
);

-- 3. TÁCTICA DE EFICIENCIA: Crear Índice Espacial GiST/R-Tree (Páginas 13, 30)
-- Permite resolver consultas por caja delimitadora (Bounding Box) en menos de 200ms
CREATE INDEX IF NOT EXISTS idx_reportes_geograficos_spatial 
ON reportes_geograficos USING gist(ubicacion);

-- 4. Insertar datos espaciales de prueba (Coordenadas de Bogotá D.C.)
INSERT INTO reportes_geograficos (report_id, tipo_daño, estado, ubicacion) VALUES
('REP-2026-BOGOTA-101', 'Hueco Calzada', 'VERIFICADO', ST_SetSRID(ST_MakePoint(-74.0817, 4.6097), 4326)),
('REP-2026-BOGOTA-102', 'Semáforo Apagado', 'ASIGNADO', ST_SetSRID(ST_MakePoint(-74.0655, 4.6486), 4326)),
('REP-2026-BOGOTA-103', 'Falta Alumbrado', 'REPARADO', ST_SetSRID(ST_MakePoint(-74.1022, 4.6152), 4326))
ON CONFLICT (report_id) DO NOTHING;

-- 5. GOBERNANZA (Pág 29, 30): Crear usuario con acceso exclusivo de SOLO LECTURA
CREATE USER analista_obras WITH PASSWORD 'read_only_gis_pass';
GRANT CONNECT ON DATABASE urban_alert_geo TO analista_obras;
GRANT USAGE ON SCHEMA public TO analista_obras;
GRANT SELECT ON TABLE reportes_geograficos TO analista_obras;
