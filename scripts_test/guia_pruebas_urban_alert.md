# Índice de pruebas Urban Alert

Esta carpeta contiene pruebas unitarias, pruebas que requieren infraestructura
local y herramientas de carga. La guía principal de ejecución integrada y sus
resultados está en [FlujoCompleto.md](../FlujoCompleto.md); los puertos,
servicios y preparación del entorno están en [README.md](../README.md).

## Quality gate local/CI

Desde la raíz del repositorio:

```powershell
python .\validar\validar_contratos.py
python -m unittest discover -s scripts_test -p "test_*.py" -v
```

El primer comando valida esquemas y ejemplos de `validar/`. El segundo ejecuta
las pruebas unitarias de Core y consumidores con dobles locales; no requiere
levantar Docker.

## Integración con Docker

Con el stack activo, ejecuta la secuencia paso a paso, incluyendo API → outbox →
RabbitMQ → PostGIS/Auditoría/Notificaciones, idempotencia, correo local y fallo
SMTP con DLQ, en [FlujoCompleto.md](../FlujoCompleto.md).

`test_notifications_redis.py`, `test_audit_persistence_db.py`,
`test_idempotency_saga.py`, `test_multimedia_nosql.py`, `test_dlq_routing.py` y
`verify_core_replication.py` requieren los servicios externos indicados por
cada script. `test_dlq_routing.py` publica eventos válidos; por sí solo no
simula una falla del proveedor SMTP.

## Carga

`stress_test_saga.py` emite eventos a RabbitMQ para observar colas y consumo.
No calcula p95 HTTP ni certifica por sí mismo los objetivos de rendimiento.
