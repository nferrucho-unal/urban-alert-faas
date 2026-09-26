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
├── postgis_init/              # Inicialización de la base geoespacial
├── fn_audit/                  # Auditoría y hash chaining
├── fn_notifications/          # Consumidor AMQP, idempotencia Redis y DLQ
├── fn_multimedia/             # Procesamiento multimedia
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

Puertos locales principales:

| Servicio | Puerto |
| --- | ---: |
| RabbitMQ AMQP | `5672` |
| RabbitMQ Management | `15672` |
| Redis | `6379` |
| MongoDB local | `27017` (solo loopback) |
| PostGIS | `5432` |
| Core primaria | `5431` |
| Core réplica | `5433` |

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
python .\scripts_test\publicar_evento_saga.py
python .\scripts_test\test_idempotency_saga.py
python .\scripts_test\test_multimedia_nosql.py
python .\scripts_test\test_notifications_redis.py
python .\scripts_test\test_dlq_routing.py
python .\scripts_test\verify_core_replication.py
```

Cada script valida un comportamiento distinto:

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