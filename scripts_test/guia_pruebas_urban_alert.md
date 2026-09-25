# GUÍA PRÁCTICA DE OPERACIÓN Y BANCO DE PRUEBAS DE ESTRÉS
## PROYECTO: URBAN ALERT - CAPA FAAS (GRUPO C)

Este documento contiene la guía paso a paso para instalar, orquestar y validar las tácticas de arquitectura de la capa de Funciones como Servicio (FaaS), caché distribuida y mensajería asíncrona de **Urban Alert**.

---

## 1. PREPARACIÓN DEL ENTORNO LOCAL

Asegúrese de estructurar el directorio de su proyecto de la siguiente forma antes de iniciar:

```text
urban-alert-faas/
│
├── docker-compose.yml          # Orquestador central de infraestructura y FaaS
├── Dockerfile                  # Imagen base optimizada de Python para las funciones
├── requirements.txt            # Dependencias globales del ecosistema
│
├── postgis_init/               # Inicialización de la persistencia espacial
│   └── init.sql
│
├── core_init/                  # Inicialización de la persistencia transaccional
│   └── init.sql
│
├── fn_audit/                   # Código FaaS de Auditoría (QAS-04.1)
│   └── app.py
│
├── fn_notifications/           # Código FaaS de Notificaciones (QAS-02)
│   └── app.py
│
├── fn_multimedia/              # Código FaaS Multimedia (QAS-03)
│   └── app.py
│
└── scripts_test/               # Banco unificado de pruebas distribuidas
    ├── publicar_evento_saga.py
    ├── test_idempotency_saga.py
    ├── test_dlq_routing.py
    ├── verify_core_replication.py
    └── stress_test_saga.py
```

### Contenido obligatorio de `requirements.txt`
```text
Flask==3.0.3
requests==2.32.3
pika==1.3.2
redis==5.0.4
psycopg2-binary==2.9.9
```

---

## 2. ORQUESTRACIÓN Y DESPLIEGUE CON DOCKER COMPOSE

Para construir las imágenes personalizadas de las funciones FaaS y encender toda la topología interconectada (incluyendo las bases de datos primarias, réplicas, caché de Redis y clúster de RabbitMQ), abra una terminal en la raíz del proyecto y ejecute:

```bash
# Construir imágenes y levantar servicios en segundo plano (Detached Mode)
docker compose up --build -d
```

### Validación de Salud de la Infraestructura (Health Checks)
La topología implementa sondas de disponibilidad rígidas. Puede verificar que todos los servicios hayan pasado satisfactoriamente las pruebas de salud ejecutando:

```bash
docker compose ps
```
*Los servicios de cómputo esperarán de forma automática a que RabbitMQ, Redis y la base de datos primaria reporten un estado `(healthy)` antes de iniciar sus escuchas.*

---

## 3. TELEMETRÍA Y LOGS ESTRUCTURADOS (FACTOR XI)

Para auditar el comportamiento del sistema bajo el formato estructurado JSON exigido por la gobernanza de observabilidad, inspeccione los flujos unificados ejecutando:

```bash
# Ver los logs en vivo de todas las funciones FaaS en paralelo
docker compose logs -f fn_audit fn_notifications fn_multimedia
```

---

## 4. BANCO DE PRUEBAS ARQUITECTÓNICAS (SCRIPTS_TEST)

Abra una segunda terminal en su máquina local, asegúrese de tener activo su entorno virtual con las dependencias instaladas y ejecute secuencialmente los siguientes escenarios de prueba:

### Escenario A: Flujo Feliz de la Saga Coreografiada asíncrona
Simula al Core registrando un reporte de emergencia y subiendo una evidencia multimedia. Valida que el Core se libere de forma instantánea (< 150 ms) delegando el procesamiento pesado en background a las tres FaaS concurrentes.
```bash
python scripts_test/publicar_evento_saga.py
```

### Escenario B: Escudo de Idempotencia en Memoria (ADR-04)
Inyecta un evento original y acto seguido envía un duplicado exacto con el mismo identificador de traza. Comprueba en los logs cómo **Redis** ataja la segunda petición en menos de 5 ms, bloqueando ejecuciones repetidas y protegiendo al ciudadano de tormentas de alertas.
```bash
python scripts_test/test_idempotency_saga.py
```

### Escenario C: Tolerancia a Fallos y Enrutamiento Automático a la DLQ (ADR-02)
Genera reportes diseñados para simular la caída del proveedor externo de mensajería (SMS/Push). Valida en la consola de administración de RabbitMQ (`http://localhost:15672`) que los mensajes sean desviados de forma automática hacia la cola de fallos `q_dead_letter_notifications` tras agotar los 3 intentos.
```bash
python scripts_test/test_dlq_routing.py
```

### Escenario D: Auditoría de Redundancia Activa y RPO (QAS-06)
Escribe un reporte de infraestructura crítica en la base transaccional primaria (Puerto 5431), espera unos milisegundos y consulta su existencia en la instancia réplica Standby (Puerto 5433). Evalúa que el RPO sea menor a un segundo.
```bash
python scripts_test/verify_core_replication.py
```

---

## 5. LABORATORIO DE ESTRÉS CONCURRENTE (PÍCOS 10X - QAS-03)

Para evaluar la capacidad de elasticidad horizontal y absorción de carga ante desastres climáticos severos, lance la ráfaga masiva ejecutando:

```bash
python scripts_test/stress_test_saga.py
```

El script mantendrá una cadencia exacta de **500 eventos por segundo en paralelo** introduciendo tanto el evento transaccional como el multimedia. Monitoree el panel de control de RabbitMQ para auditar el drenado simultáneo de los workers FaaS sin registrar degradación o pérdida de consistencia en el sistema.
