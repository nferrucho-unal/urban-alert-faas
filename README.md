# Urban Alert: FaaS y persistencia aislada

Guía de operación local para la plataforma Urban Alert. El proyecto reúne funciones FaaS, mensajería asíncrona con RabbitMQ, caché e idempotencia con Redis, bases de datos PostgreSQL y una réplica en caliente para validar disponibilidad.

## Inicio rápido

### 1. Preparación del entorno local

Requisitos:

- Docker Desktop con Docker Compose.
- Python 3.12 o superior.
- PowerShell en Windows o una terminal compatible.

Desde la raíz del proyecto, crea y activa el entorno virtual e instala las dependencias:

```powershell
python -m venv env
.\env\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

Las dependencias principales son `pika`, Redis, PyMongo y `psycopg2-binary`.

Estructura funcional:

```text
urban-alert-faas/
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
├── core_init/                 # Esquema y migraciones de Core
├── audit_init/                # Event Store PostgreSQL append-only
├── core_services/             # APIs de Reportes, Usuarios, Obras y Geoespacial
├── postgis_init/              # Inicialización de la base geoespacial
├── fn_audit/                  # Auditoría y hash chaining
├── fn_notifications/          # Consumidor AMQP, idempotencia Redis y DLQ
├── fn_multimedia/             # Procesamiento multimedia
├── validar/                   # Contratos y esquemas JSON versionados
└── scripts_test/              # Scripts de validación y estrés
```

RabbitMQ, Redis, Mailpit, MongoDB y las bases PostgreSQL/PostGIS son servicios
de Compose, no carpetas del repositorio.

### 2. Orquestación con Docker Compose

Construye las imágenes y levanta toda la topología:

```powershell
docker compose up --build -d
```

Comprueba el estado de los servicios:

```powershell
docker compose ps
```

RabbitMQ conserva su estado en el volumen nombrado `rabbitmq_data`. El entrypoint
inicia como root solo para corregir ownership de ese volumen y luego ejecuta el
broker como usuario sin privilegios `rabbitmq`.

Puertos locales principales:

| Servicio | Puerto |
| --- | ---: |
| RabbitMQ AMQP | `5672` |
| RabbitMQ Management | `15672` |
| SMTP local (Mailpit) | `1025` |
| Bandeja Mailpit | `8025` |
| S3 local compatible (SeaweedFS) | `8333` |
| Consola SeaweedFS (UI) | `9333` |
| Redis | `6379` |
| MongoDB local | `27017` (solo loopback) |
| PostGIS | `5432` |
| Core primaria | `5431` |
| Core réplica | `5433` |
| API Core (Reportes/Usuarios/Obras) | `5001` |
| API Geoespacial | `5002` |

Compose aplica `core_init/002_business_services.sql`,
`core_init/003_notification_reader.sql` y `postgis_init/002_reportes_geo.sql`
antes de iniciar los servicios dependientes. No es necesario borrar volúmenes
para actualizar el esquema.
Auditoría conserva sus eventos en PostgreSQL dedicado (`audit_db`) sobre el
volumen `audit_data`. `audit_events` es append-only y deduplica por `eventId`;
un trigger impide UPDATE/DELETE. El consumidor usa `audit_writer` (SELECT e
INSERT, sin mutación de eventos) y la API usa `audit_reader` (solo SELECT).
`audit_migrate` crea ambos roles e índices por reporte/secuencia y correlación.
La cadena SHA-256 se actualiza dentro de la transacción que inserta cada evento.

El recorrido comprobado de API, outbox, RabbitMQ, PostGIS, Auditoría y
Notificaciones, las consultas de auditoría y el procedimiento de retry/DLQ
están en [FlujoCompleto.md](FlujoCompleto.md). Allí también se distingue lo
verificado localmente de lo que requiere configuración cloud.

### Servicios disponibles

- `POST /reportes`, `GET /reportes/{id}` y `GET /reportes?bbox=...`: creación,
	consulta y listado con filtros; las consultas espaciales se delegan a PostGIS.
- `PATCH /reportes/{id}/estado` y `POST /obras`: transiciones y asignaciones
	transaccionales con eventos en outbox.
- `/usuarios/me` y `/usuarios/{id}/rol`: perfil ciudadano y administración de
	roles.
- `/reportes/{id}/multimedia/*`: carga presignada, confirmación y descarga
	temporal. El detalle de rutas y respuestas está en
	[validar/servicio_reportes_diseno.md](validar/servicio_reportes_diseno.md).
- `/auditoria/reportes/{id}` y `/geospatial/reportes`: historial/integridad y
	consulta GeoJSON; el runbook enlazado arriba contiene ejemplos de uso.

En local, Compose enlaza Core a loopback, permite `X-User-Context` solo para
pruebas (`ALLOW_TEST_USER_CONTEXT=true`) y configura una clave Gateway local de
desarrollo que debes enviar en `X-Urban-Gateway-Key`.
En producción, el backend verifica firma, issuer, cliente, expiración y `token_use`
del JWT Cognito. Resuelve `sub` de la identidad validada y usa rol/municipio de
PostgreSQL cuando ya existe perfil, por lo que los cambios de rol son efectivos
en la siguiente solicitud aunque el token no haya expirado. Nunca se confía en un
`X-User-Context` enviado por el cliente.

Notificaciones resuelve el email del propietario del reporte desde Core con el
rol de solo lectura `notification_reader` y envía correo mediante SMTP. En
Compose, Mailpit captura los mensajes en <http://localhost:8025> sin entregarlos
a destinatarios externos. Para producción, configura `SMTP_HOST`, `SMTP_PORT`,
`SMTP_FROM_EMAIL`, `SMTP_STARTTLS=true`, `SMTP_USERNAME` y `SMTP_PASSWORD` con
credenciales de un relay como Amazon SES SMTP o SendGrid; guarda las credenciales
en el gestor de secretos del despliegue, no en Compose ni en el repositorio.

API Gateway inyecta `X-Urban-Gateway-Key`; el backend rechaza cualquier solicitud
sin la clave constante configurada como `gateway_shared_secret`. Esto impide usar
el endpoint público de App Runner como bypass de Gateway/WAF. Provisiona el valor
con un secreto aleatorio de al menos 32 caracteres vía `TF_VAR_gateway_shared_secret`
o un mecanismo de secretos del pipeline; no lo guardes en el repositorio ni en un
archivo `.tfvars` versionado. Restringe el acceso al state de Terraform, que contiene
el valor durante el despliegue.

Con PowerShell puedes generar un valor temporal para tu pipeline con:

```powershell
$env:TF_VAR_gateway_shared_secret = [Convert]::ToBase64String([Security.Cryptography.RandomNumberGenerator]::GetBytes(48))
```

Guárdalo en el almacén de secretos del CI para despliegues repetibles y rótalo
coordinando un nuevo `terraform apply`; el stage de API Gateway se redepliega al
cambiar el mapping.

### 3. Telemetría y logs

Logs en vivo de las tres funciones FaaS:

```powershell
docker compose logs -f fn_audit fn_notifications fn_multimedia
```

Logs de un servicio específico:

```powershell
docker compose logs -f fn_audit
docker compose logs -f fn_notifications
docker compose logs -f fn_multimedia
```

Para consultar las últimas líneas sin mantener el seguimiento abierto:

```powershell
docker compose logs --tail 100
```

La consola de administración de RabbitMQ está disponible en <http://localhost:15672> con las credenciales definidas en `docker-compose.yml`.

### Verificación

Desde la raíz, con el entorno virtual activo, ejecuta el mismo gate local que
usa CI:

```powershell
python .\validar\validar_contratos.py
python -m unittest discover -s scripts_test -p "test_*.py" -v
```

Para los recorridos que requieren infraestructura real (API, outbox, RabbitMQ,
PostGIS, Auditoría, Redis, SMTP/Mailpit, retries y DLQ), sigue
[FlujoCompleto.md](FlujoCompleto.md). La guía histórica de pruebas queda como
índice en [scripts_test/guia_pruebas_urban_alert.md](scripts_test/guia_pruebas_urban_alert.md).

El emisor de carga se ejecuta con:

```powershell
python .\scripts_test\stress_test_saga.py
```

Genera una ráfaga de eventos para observar colas y consumidores; no mide por sí
solo el p95 de `POST /reportes` ni certifica el SLO de rendimiento.

Para detener los contenedores sin eliminar los volúmenes persistentes:

```powershell
docker compose down
```