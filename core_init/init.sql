-- 1. Crear tablas del núcleo transaccional (CORE)
CREATE TABLE IF NOT EXISTS usuarios (
    id SERIAL PRIMARY KEY,
    username VARCHAR(50) UNIQUE NOT NULL,
    rol VARCHAR(30) NOT NULL
);

CREATE TABLE IF NOT EXISTS reportes_core (
    id SERIAL PRIMARY KEY,
    report_id VARCHAR(50) UNIQUE NOT NULL,
    descripcion TEXT NOT NULL,
    estado VARCHAR(30) DEFAULT 'CREADO' NOT NULL,
    fecha_creacion TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS obras (
    id SERIAL PRIMARY KEY,
    obra_id VARCHAR(50) UNIQUE NOT NULL,
    report_id VARCHAR(50) REFERENCES reportes_core(report_id),
    entidad_responsable VARCHAR(100) NOT NULL,
    presupuesto_asignado NUMERIC(12,2)
);

-- 2. GOBERNANZA (Pág 29): El usuario de la app no debe tener privilegios DDL
CREATE USER app_core_user WITH PASSWORD 'app_secure_pass';
GRANT CONNECT ON DATABASE urban_alert_core TO app_core_user;
GRANT USAGE ON SCHEMA public TO app_core_user;
GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA public TO app_core_user;
GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO app_core_user;
