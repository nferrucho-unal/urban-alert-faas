# Diseño del Servicio de Reportes — Urban Alert

Este documento resuelve los 7 puntos abiertos para construir `ServicioReportes`, coherente con la arquitectura del DSL (`LoadBalancer -> ServicioReportes`, patrón *Publish/Subscribe* sobre RabbitMQ) y con el objetivo de calidad citado en `delivery3-team-C-revisión.md:160` (p95 < 150 ms, cero reportes perdidos en picos) y `delivery3-team-C-revisión.md:210` (aislar las consultas geoespaciales de la creación de reportes).

> **Estado actualizado del repositorio (2026-09-30):** Core está implementado en `core_services/` y Docker Compose levanta la API, el relay outbox, el projector geoespacial y los consumidores. `core_init/002_business_services.sql`, `003_notification_reader.sql` y `postgis_init/002_reportes_geo.sql` definen las migraciones locales. PostGIS es una proyección asíncrona y separada. Esta especificación conserva decisiones y metas de diseño; el estado de pruebas medido se registra en `FlujoCompleto.md`.

---

## 1. Contrato de API y reglas de negocio

### 1.1 Roles

| Rol | Origen del claim JWT | Puede |
|---|---|---|
| `ciudadano` | `IdentityProvider` (OIDC) | Crear, consultar y listar **sus propios** reportes |
| `gestor` | `IdentityProvider` | Consultar/listar todos los reportes de su municipio, transicionar estado |
| `admin` | `IdentityProvider` | Todo lo anterior sin restricción de municipio |
| `sistema` (service-to-service) | client credentials | Puede usar las transiciones soportadas por la API de estados; la asignación de obra actual se expone a `gestor`/`admin` |

En producción, API Gateway aplica Cognito y añade `X-Urban-Gateway-Key`; Core verifica RS256, issuer, cliente, expiración y `token_use`, y resuelve el rol y municipio autorizados desde Core. `X-User-Context` solo está habilitado para pruebas locales (`ALLOW_TEST_USER_CONTEXT=true`), no como identidad de producción.

### 1.2 Endpoints

| Método y ruta | Rol mínimo | Descripción |
|---|---|---|
| `POST /reportes` | `ciudadano` | Crea un reporte en estado `RECIBIDO` |
| `GET /reportes/{id}` | `ciudadano` (dueño) / `gestor` / `admin` | Consulta un reporte |
| `GET /reportes?bbox=&estado=&categoria=&desde=&hasta=&cursor=` | `gestor` / `admin` | Lista/filtra por bounding box `bbox=minLon,minLat,maxLon,maxLat`, estado, categoría y rango de fechas; paginado por cursor |
| `PATCH /reportes/{id}/estado` | `gestor` / `admin` / `sistema` | Transiciona el estado (body: `{estado, motivo}`) |
| `POST /reportes/{id}/multimedia` | `ciudadano` (dueño) | Registra una referencia a un archivo ya subido vía URL prefirmada (ver §5) |
| `POST /reportes/{id}/multimedia/upload-url` | `ciudadano` (dueño) | Crea una carga temporal presignada en S3/SeaweedFS |

La implementación usa `bbox` (no `zona`): Core reenvía el filtro espacial a `ServicioGeoespacial` y restringe sus metadatos a los IDs recibidos. Así se mantiene aislada la consulta geoespacial de la ruta de escritura.

### 1.3 Campos obligatorios de creación (`POST /reportes`)

```json
{
  "categoria": "hueco_via | fuga_agua | alumbrado | arbolado | otro",
  "descripcion": "string, 10–500 caracteres",
  "ubicacion": { "lat": -90..90, "lon": -180..180 },
  "municipio_id": "uuid, debe existir en catálogo de municipios",
  "correlationId": "uuid, generado por el cliente (idempotencia, ver §4)"
}
```

`actor_id` **no** viaja en el body: se toma del contexto autenticado para evitar suplantación.

### 1.4 Máquina de estados

```
RECIBIDO ──validar──> VALIDADO ──asignar──> EN_OBRA ──cerrar──> RESUELTO
   │                      │                                        
   └──────rechazar────────┴───────────────────────────────> RECHAZADO
```

- Transiciones válidas se aplican con `estado_actual → estado_nuevo` en una tabla de reglas en código (no en la BD); cualquier otra combinación devuelve `409 Conflict`.
- Solo `gestor`/`admin`/`sistema` transicionan; el ciudadano puede crear y consultar sus reportes, pero no cambiar su estado.
- Cada transición genera una fila en `reporte_historial_estado` y el evento correspondiente en outbox dentro de la transacción. La ruta de estado publica `reporte.validado`, `reporte.rechazado` o `reporte.resuelto`; `POST /obras` establece `EN_OBRA` y publica `obra.asignada`.
- **Brecha de autorización:** aunque la tabla y la transición contemplan `sistema`, `POST /obras` hoy requiere `gestor` o `admin`; el acceso service-to-service de Obras no está implementado en esa ruta.

### 1.5 Respuestas de error (uniformes)

```json
{ "error": { "code": "REPORTE_NOT_FOUND", "message": "...", "correlationId": "..." } }
```

| HTTP | code | Causa |
|---|---|---|
| 400 | `VALIDATION_ERROR` | Campo faltante o fuera de rango |
| 401 | `UNAUTHENTICATED` | Falta o expiró el JWT (no debería llegar aquí; lo filtra el gateway) |
| 403 | `FORBIDDEN` | Rol o municipio no autorizado para el recurso |
| 404 | `REPORTE_NOT_FOUND` | Id inexistente o de otro ciudadano |
| 409 | `INVALID_STATE_TRANSITION` | Transición de estado no permitida |
| 422 | `MUNICIPIO_NOT_FOUND` | `municipio_id` no existe en el catálogo |
| 500 | `INTERNAL_ERROR` | No exponer detalles internos ni stack traces |

---

## 2. Modelo de datos completo

### 2.1 Core — PostgreSQL (`reportes_core`, escritura autoritativa)

El esquema implementado conserva la tabla `reportes_core` y agrega `categoria`, `actor_id`, `municipio_id`, `lat`, `lon`, `correlation_id` y `actualizado_en`, además de historial, multimedia y outbox. La migración vigente es `core_init/002_business_services.sql`; la tabla identifica al reporte por `report_id`.

```sql
ALTER TABLE reportes_core
    ADD COLUMN categoria       TEXT NOT NULL DEFAULT 'otro'
                                 CHECK (categoria IN ('hueco_via','fuga_agua','alumbrado','arbolado','otro')),
    ADD COLUMN actor_id        UUID,               -- sub del JWT de quien creó el reporte; back-fill antes de poner NOT NULL
    ADD COLUMN municipio_id    UUID REFERENCES municipios(id),
    ADD COLUMN lat             DOUBLE PRECISION CHECK (lat BETWEEN -90 AND 90),
    ADD COLUMN lon             DOUBLE PRECISION CHECK (lon BETWEEN -180 AND 180),
    ADD COLUMN correlation_id  UUID,               -- idempotencia de creación (ver §4)
    ADD COLUMN actualizado_en  TIMESTAMPTZ NOT NULL DEFAULT now();

ALTER TABLE reportes_core
    ADD CONSTRAINT chk_descripcion_len CHECK (char_length(descripcion) BETWEEN 10 AND 500);

CREATE UNIQUE INDEX ux_reportes_correlation ON reportes_core(correlation_id);
CREATE INDEX ix_reportes_municipio_estado ON reportes_core(municipio_id, estado);
CREATE INDEX ix_reportes_actor ON reportes_core(actor_id);
```

`actor_id`, `municipio_id`, `lat/lon` y `correlation_id` quedan nulables en la migración (para no romper filas existentes) y se vuelven `NOT NULL` en una segunda migración una vez hecho el back-fill — o directamente `NOT NULL` si la tabla todavía no tiene datos de producción.

```sql
CREATE TABLE reporte_historial_estado (
    id            BIGSERIAL PRIMARY KEY,
    reporte_id    UUID NOT NULL REFERENCES reportes_core(id),
    estado_previo TEXT,
    estado_nuevo  TEXT NOT NULL,
    actor_id      UUID NOT NULL,
    motivo        TEXT,
    creado_en     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE reporte_multimedia (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    reporte_id    UUID NOT NULL REFERENCES reportes_core(id),
    object_key    TEXT NOT NULL,      -- clave en el bucket S3; ver nota de fn_multimedia más abajo
    mime_type     TEXT NOT NULL,
    tamano_bytes  BIGINT,
    creado_en     TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Los metadatos ricos (EXIF, dimensiones) ya se persisten en MongoDB por el consumidor
-- fn_multimedia existente, que procesa el evento `multimedia.upload`; aquí solo se
-- guarda la referencia mínima para no duplicar el dominio de esa función.
```

`lat/lon` **sí** se guardan en Core (no solo en PostGIS): el listado por municipio/estado y la vista de un reporte individual no deben depender de una segunda base para mostrar el punto en el mapa de detalle. PostGIS guarda la geometría indexada para *consultas* espaciales (bounding box, cercanía), no como única fuente de la coordenada.

### 2.2 PostGIS (proyección de solo consulta)

Compose inicializa `reportes_geo` con índice GiST. `analista_obras` conserva lectura para la API geoespacial; `svc_geo_projector` es el rol separado de escritura de la proyección. El esquema vigente es `postgis_init/002_reportes_geo.sql`:

```sql
CREATE TABLE reportes_geo (
    reporte_id   UUID PRIMARY KEY,          -- mismo id que reportes_core.id, sin FK físico (bases distintas)
    municipio_id UUID NOT NULL,
    categoria    TEXT NOT NULL,
    estado       TEXT NOT NULL,
    geom         GEOMETRY(Point, 4326) NOT NULL,
    creado_en    TIMESTAMPTZ NOT NULL
);
CREATE INDEX ix_reportes_geo_gist ON reportes_geo USING GIST (geom);
```

Esta tabla es una **réplica de lectura** poblada de forma asíncrona (§3), no la tabla donde escribe `POST /reportes`. `analista_obras` sigue siendo de solo lectura sobre ella — **no se le debe ampliar el permiso**; el rol de escritura del projector es uno nuevo, acotado (§3 y §6).

---

## 3. Coordinación entre Core (Postgres) y PostGIS

Postgres Core y PostGIS son bases **separadas** (confirmado en el DSL: `ServicioReportes → PostgreSQL` vs. `ServicioGeoespacial → PostGIS`, y confirmado en el repo: el rol `analista_obras` sobre PostGIS es de solo lectura). Se resuelve así, sin dos-phase-commit entre bases distintas:

1. `POST /reportes` escribe **una sola transacción local** en Core: `reportes_core` + `reporte_historial_estado` + fila en `outbox_eventos` (ver §4). Postgres Core es la única fuente de verdad.
2. El projector asíncrono consume `reporte.creado` y `reporte.validado` desde RabbitMQ y actualiza `reportes_geo` con `svc_geo_projector`; `analista_obras` se mantiene de solo lectura.
3. `ServicioGeoespacial` consulta exclusivamente `reportes_geo` con `analista_obras` (solo lectura) — nunca toca `reportes_core`. Esto es lo que aísla la ruta de consulta geoespacial de la ruta de creación (`delivery3-team-C-revisión.md:210`): un pico de consultas de mapa no compite por locks ni por pool de conexiones con la escritura de reportes nuevos. El límite de 50 conexiones ya configurado para PostGIS en `docker-compose.yml:119` reduce aún más ese riesgo de contención.
4. La proyección en PostGIS es eventual: `GET /reportes/{id}` lee Core, mientras que el mapa puede tardar en reflejar un reporte. El projector reintenta fallos con contador Redis y envía a `q_dead_letter_geospatial` al quinto intento.

---

## 4. Eventos confiables (Outbox transaccional + consumidores idempotentes)

### 4.1 Por qué outbox

Si el servicio primero hace `COMMIT` en Postgres y luego publica en RabbitMQ, una caída entre esos dos pasos pierde el evento sin perder el reporte — viola "cero reportes perdidos". El patrón *outbox* evita esa ventana: el evento se escribe **en la misma transacción** que el reporte.

```sql
CREATE TABLE outbox_eventos (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tipo           TEXT NOT NULL,           -- 'reporte.creado', 'reporte.validado', ...
    payload        JSONB NOT NULL,
    correlation_id UUID NOT NULL,
    creado_en      TIMESTAMPTZ NOT NULL DEFAULT now(),
    publicado_en   TIMESTAMPTZ
);
CREATE INDEX ix_outbox_pendientes ON outbox_eventos(creado_en) WHERE publicado_en IS NULL;
```

Un *relay* (poller cada ~200 ms, o `pg_logical`/Debezium si el equipo prefiere CDC) lee filas con `publicado_en IS NULL`, publica en el exchange de RabbitMQ (`Publish/Subscribe`, tópico `reporte.*`) y marca `publicado_en = now()`. Si el relay muere a mitad de camino, el mensaje sigue pendiente y se reintenta — nunca se pierde porque nunca se borró de `outbox_eventos` hasta confirmar la publicación (ack del broker).

### 4.2 Esquema del evento `reporte.creado`

```json
{
  "eventId": "uuid",
  "eventType": "reporte.creado",
  "version": 1,
  "occurredAt": "2026-09-26T14:03:00Z",
  "correlationId": "uuid",
  "reportId": "uuid",
  "data": {
    "actorId": "uuid",
    "municipioId": "uuid",
    "categoria": "hueco_via",
    "lat": 4.65,
    "lon": -74.05
  }
}
```

El esquema versionado y los ejemplos válidos viven en `validar/schemas/` y `validar/ejemplos/`.

`correlationId` es el mismo que generó el cliente en `POST /reportes` (§1.3): permite seguir un reporte de punta a punta entre `ServicioReportes`, `ServicioAuditoria` (Structured Logging + Correlation IDs, ya presente en el DSL) y `ServicioNotificaciones`.

### 4.3 Idempotencia

- **En la creación:** `ux_reportes_correlation` (índice único, §2.1) hace que un reintento del cliente con el mismo `correlationId` devuelva el reporte ya creado (`200` con el recurso existente) en vez de duplicarlo.
- **En los consumidores:** Notificaciones y el projector usan claves Redis con TTL de 24 h; Auditoría deduplica por `eventId` con la restricción única del event store PostgreSQL. Todos validan eventos con los esquemas compartidos.

---

## 5. Multimedia (URLs prefirmadas)

`ServicioReportes` **no** recibe binarios. Flujo:

1. Cliente llama `POST /reportes/{id}/multimedia/upload-url` → Core genera una URL POST presignada de vigencia corta (5 min en local/configuración actual) hacia S3/SeaweedFS.
2. El cliente sube el archivo directo a Object Storage con esa URL (nunca pasa por `ServicioReportes` ni por `PostgreSQL`).
3. Cliente confirma con `POST /reportes/{id}/multimedia` enviando `uploadId`; Core valida HEAD, tamaño, metadatos y firma del contenido, promueve el objeto, persiste la referencia y `multimedia.upload` en outbox.

Esto es exactamente el desacoplamiento de persistencia que ya contempla el escenario de escalabilidad del proyecto (evitar guardar archivos grandes en la base transaccional).

---

## 6. Seguridad y operación

- **Validación de entrada:** cada campo del §1.3 se valida (tipo, rango, longitud, enum) **antes** de tocar la base — rechazo temprano con `400`, sin ejecutar ninguna consulta.
- **Consultas parametrizadas siempre** (ORM o `psycopg2` con placeholders `%s`), nunca interpolación de strings — es la mitigación que ya aplica el WAF en el borde, pero el servicio no debe depender solo de esa capa (defensa en profundidad).
- **Privilegio mínimo por base:**
  - Core usa `app_core_user`; la migración actual concede operaciones de negocio sobre varias tablas Core y no `DELETE`/DDL.
  - `svc_geo_projector` tiene escritura acotada a `reportes_geo`; `analista_obras` conserva lectura.
  - `analista_obras` **no cambia**: sigue siendo de solo lectura y sigue siendo el rol que consulta `ServicioGeoespacial`.
- **Stateless:** el servicio no guarda sesión ni estado local; cualquier instancia detrás del `LoadBalancer` puede atender cualquier solicitud (coherente con la táctica `Stateless Services` ya modelada). La configuración de conexión (host/usuario/secreto de Postgres Core, PostGIS, Redis, RabbitMQ) llega por variables de entorno, una por dependencia, resueltas según el entorno (local vía `docker-compose.yml`, nube vía el mecanismo de secretos de App Runner).
- **RBAC:** se aplica dos veces — grueso en el `ApiGateway` (¿el rol puede llegar a esta ruta?) y fino en el servicio (¿este `gestor` pertenece al `municipio_id` del reporte?).
- **Estado de despliegue:** Core API sí corre localmente en Compose. Terraform define recursos de App Runner/API Gateway, pero las pruebas locales no certifican un despliegue productivo completo ni el cumplimiento de los SLO cloud.

---

## 7. Plan de pruebas

| Área | Prueba | Objetivo |
|---|---|---|
| Creación/consulta | Crear reporte válido → `201`; consultar por id → `200` con los mismos datos | Camino feliz |
| Creación/consulta | Crear con `categoria` inválida, `descripcion` de 3 caracteres, `lat=200` | `400 VALIDATION_ERROR` en cada caso |
| Autenticación | Llamar sin header `X-User-Context` | El servicio rechaza (`401`) aunque en producción el gateway ya filtró esto |
| Autorización | `ciudadano` A consulta un reporte de `ciudadano` B | `404` (no `403`, para no confirmar existencia) |
| Autorización | `gestor` de municipio X consulta reporte de municipio Y | `403` |
| Transición de estado | `RECIBIDO → RESUELTO` directo | `409 INVALID_STATE_TRANSITION` |
| Inyección | `descripcion` con payload `'; DROP TABLE reportes; --` | Se persiste como texto literal, la tabla sigue intacta |
| Consistencia ubicación/evento | Crear reporte → esperar al projector → `reportes_geo` contiene el mismo `reporte_id`, `lat/lon` coherentes | Verifica §3 end-to-end |
| Reintentos/idempotencia | `POST /reportes` dos veces con el mismo `correlationId` | Una sola fila en `reportes`, segunda respuesta devuelve el mismo recurso |
| Reintentos/idempotencia | Publicar el mismo evento dos veces al consumidor de notificaciones | Una sola notificación enviada (dedupe por `eventId` en Redis) |
| Resiliencia | Detener RabbitMQ tras el `COMMIT` de un `POST /reportes` | El evento queda en `outbox_eventos` con `publicado_en IS NULL`; al reiniciar el broker, el relay lo publica sin pérdida |
| Rendimiento | Carga sostenida sobre `POST /reportes` (k6/locust) | p95 < 150 ms end-to-end, 0 respuestas 5xx, 0 eventos sin publicar al final de la corrida |

Los tests unitarios de productores/autorización están en `scripts_test/test_business_producers.py`; el flujo local medido y el caso SMTP/DLQ están en `FlujoCompleto.md`. Siguen pendientes una prueba de caída de RabbitMQ después del commit, la verificación de recuperación del outbox en ese escenario y un benchmark k6/Locust que mida p95 < 150 ms bajo carga. Por tanto, esos objetivos siguen siendo metas, no resultados certificados.
