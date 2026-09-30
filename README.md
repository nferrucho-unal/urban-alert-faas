# Urban Alert: FaaS y persistencia aislada

Guía de operación local para la plataforma Urban Alert. El proyecto reúne funciones FaaS, mensajería asíncrona con RabbitMQ, caché e idempotencia con Redis, bases de datos PostgreSQL y una réplica en caliente para validar disponibilidad.

## Los cinco elementos de la guía

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
├── core_init/                 # Esquema y permisos de la base Core
├── core_services/             # APIs de Reportes, Usuarios, Obras y Geoespacial
├── postgis_init/              # Inicialización de la base geoespacial
├── fn_audit/                  # Auditoría y hash chaining
├── fn_notifications/          # Consumidor AMQP, idempotencia Redis y DLQ
├── fn_multimedia/             # Procesamiento multimedia
├── validar/                   # Contratos y esquemas JSON versionados
├── MongoDB                    # Metadata multimedia NoSQL
└── scripts_test/              # Scripts de validación y estrés
```

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
| S3 local compatible (SeaweedFS) | `8333` |
| Consola SeaweedFS (UI) | `9333` |
| Redis | `6379` |
| MongoDB local | `27017` (solo loopback) |
| PostGIS | `5432` |
| Core primaria | `5431` |
| Core réplica | `5433` |
| API Core (Reportes/Usuarios/Obras) | `5001` |
| API Geoespacial | `5002` |

Compose aplica `core_init/002_business_services.sql` y
`postgis_init/002_reportes_geo.sql` con tareas idempotentes antes de iniciar
las APIs. No es necesario borrar los volúmenes para actualizar el esquema.

### Servicios de negocio

- `POST /reportes`: crea en estado `RECIBIDO`, exige categoria, descripcion,
	ubicacion, municipio_id y correlationId. La transacción guarda el reporte y
	`reporte.creado` en outbox.
- `GET /reportes/{uuid}` y `GET /reportes?...`: consulta/lista; el bbox se
	delega al Servicio Geoespacial.
- `PATCH /reportes/{uuid}/estado`: permite `RECIBIDO -> VALIDADO` y genera
	`reporte.validado` en outbox.
- `GET|PUT /usuarios/me`: consulta/actualiza el perfil ciudadano; no permite
	autoasignar roles.
- `PATCH /usuarios/{uuid}/rol`: admin cambia un rol y produce
	`usuario.rol_cambiado` en outbox.
- `POST /obras`: asigna una obra a un reporte validado, cambia su estado a
	`EN_OBRA` y produce `obra.asignada` en la misma transacción.
- `POST /reportes/{uuid}/multimedia/upload-url`: solicita una policy POST de
	S3 por cinco minutos para JPEG, PNG o WebP de hasta 15 MiB. En local la
	implementa SeaweedFS; en producción se usa Amazon S3. Devuelve
	`uploadUrl`, `formFields` y `fileField`; el cliente agrega primero todos los
	campos al formulario `multipart/form-data` y el archivo en `file`.
- `POST /reportes/{uuid}/multimedia`: confirma `{ "uploadId": "..." }`; el
	servicio verifica el objeto, lo mueve de `uploads/pending/` a `reports/`, y
	registra la referencia y `multimedia.upload` en la misma transacción outbox.
- `GET /reportes/{uuid}/multimedia/{uploadId}/download-url`: genera una URL GET
	temporal después de autorizar al usuario. El bucket permanece privado.
- `GET /geospatial/reportes?bbox=minLon,minLat,maxLon,maxLat`: devuelve un
	GeoJSON desde la proyección aislada de PostGIS.

En local, Compose enlaza Core a loopback, permite `X-User-Context` solo para
pruebas (`ALLOW_TEST_USER_CONTEXT=true`) y configura una clave Gateway local de
desarrollo que debes enviar en `X-Urban-Gateway-Key`.
En producción, el backend verifica firma, issuer, cliente, expiración y `token_use`
del JWT Cognito. Resuelve `sub` de la identidad validada y usa rol/municipio de
PostgreSQL cuando ya existe perfil, por lo que los cambios de rol son efectivos
en la siguiente solicitud aunque el token no haya expirado. Nunca se confía en un
`X-User-Context` enviado por el cliente.

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

`PATCH estado=RECHAZADO` responde `409 CONTRACT_NOT_DEFINED`: falta aprobar un
esquema para `reporte.rechazado`. No se persiste una transición que no pueda
auditarse por evento.

Para detener los contenedores y la red del proyecto:

```powershell
docker compose down
```

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

### 4. Banco de pruebas arquitectónicas

Con los servicios levantados y el entorno virtual activo, ejecuta los scripts desde la raíz del proyecto:

```powershell
python .\validar\validar_contratos.py
python .\scripts_test\test_auth_boundary.py
python .\scripts_test\test_contract_consumers.py
python .\scripts_test\test_business_producers.py
python .\scripts_test\test_multimedia_flow.py
python .\scripts_test\publicar_evento_saga.py
python .\scripts_test\test_idempotency_saga.py
python .\scripts_test\test_multimedia_nosql.py
python .\scripts_test\test_notifications_redis.py
python .\scripts_test\test_dlq_routing.py
python .\scripts_test\verify_core_replication.py
```

Cada script valida un comportamiento distinto:

- `validar_contratos.py`: valida los ejemplos contra los esquemas versionados.
- `test_auth_boundary.py`: verifica JWT RS256, issuer/cliente/expiración, secreto Gateway y rol vigente en DB.
- `test_contract_consumers.py`: verifica validación, DLQ y versiones desconocidas en los consumidores.
- `test_business_producers.py`: valida contratos de productores, RBAC básico, validación de entradas y relay outbox.
- `test_multimedia_flow.py`: verifica firma de carga, HEAD/metadata, promoción a clave final y URL de descarga.
- `test_multimedia_flow.py`: verifica firma POST, restricciones de objeto, confirmación y URL privada de descarga.
- `publicar_evento_saga.py`: flujo feliz de la saga y propagación de eventos.
- `test_idempotency_saga.py`: detección de un mensaje duplicado mediante Redis.
- `test_multimedia_nosql.py`: publica un evento multimedia y verifica su metadata en MongoDB.
- `test_notifications_redis.py`: publica un reporte y valida idempotencia/estado con TTL en Redis.
- `test_dlq_routing.py`: publicación de eventos para validar el enrutamiento a DLQ.
- `verify_core_replication.py`: escritura en la primaria, lectura en la réplica y bloqueo de escrituras en standby.

Si no deseas activar el entorno virtual, usa directamente su intérprete:

```powershell
.\env\Scripts\python.exe .\scripts_test\verify_core_replication.py
```

### 5. Laboratorio de estrés concurrente

Ejecuta la prueba de carga con los servicios activos:

```powershell
python .\scripts_test\stress_test_saga.py
```

Mientras corre la prueba, observa los logs de las funciones y las métricas de RabbitMQ para verificar el consumo concurrente, la acumulación de mensajes y la ausencia de errores.

## Flujo recomendado completo

```powershell
.\env\Scripts\Activate.ps1
python -m pip install -r requirements.txt
docker compose up --build -d
docker compose ps
python .\scripts_test\test_idempotency_saga.py
python .\scripts_test\test_dlq_routing.py
python .\scripts_test\verify_core_replication.py
docker compose logs --tail 100
```

Al terminar el trabajo:

```powershell
docker compose down
deactivate
```