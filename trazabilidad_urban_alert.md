# DOCUMENTACIÓN TÉCNICA CONSOLIDADA: URBAN ALERT (GRUPO C)

Este documento recopila la trazabilidad técnica, el análisis de compensaciones (*trade-offs*) y los diagramas conceptuales de flujos para la plataforma **Urban Alert**.

---

## 1. MATRIZ CONSOLIDADA DE TRAZABILIDAD TÉCNICA

A continuación, se presenta la vinculación directa de los componentes del sistema con las tácticas arquitectónicas aplicadas, sus equivalentes implementados en los servicios gestionados de AWS y sus respectivos parámetros de gobernanza.

| Componente de Software | Tácticas Arquitectónicas Respaldadas | Servicio en la Nube (AWS) | Parámetros de Gobernanza Aplicados (IaC) |
| :--- | :--- | :--- | :--- |
| **Ingress / Borde Perimetral** | • Authenticate actors<br>• Authorize actors (RBAC)<br>• Limit Access (Token Bucket) | **Managed API Gateway + AWS WAF** | • TLS 1.3 estricto.<br>• Reglas base OWASP en WAF.<br>• Token Bucket limitado a 100 req/s por IP/cliente.<br>• Rechazo HTTP 401/403/429 en < 5ms. |
| **Identity Provider** | • Authenticate actors<br>• Authorize actors | **Amazon Cognito (IDaaS)** | • Firma asimétrica JWT RS256.<br>• Rotación de llaves automática.<br>• Configuración de tiempo de vida (TTL) mínimo del token de 5 a 15 minutos. |
| **Servicios Core** *(Usuarios, Reportes, Obras)* | • Autoescalado reactivo<br>• Stateless Design<br>• Load Balancing | **Serverless Containers (Amazon App Runner)** | • Auto Scaling Group (ASG): Mín. 2 – Máx. 20 instancias.<br>• Disparado por CPU > 70% o profundidad de colas.<br>• Cold-start aceptado de ~1.2s. |
| **Servicio de Notificaciones** | • Failure Detection (Health Checks)<br>• Circuit Breaking (3 fallos)<br>• Retry with Exponential Backoff + Jitter<br>• Dead Letter Enqueueing (DLQ) | **AWS Lambda (FaaS Dispatcher)** | • Memoria asignada: 256–512 MB.<br>• Timeout rígido: 10 segundos.<br>• Aislamiento por rol de ejecución IAM acotado exclusivamente a servicios de salida (SES/SNS/FCM). |
| **Servicio de Auditoría** | • Event Streaming<br>• Append-Only Event Store<br>• Structured Logging (JSON)<br>• Distributed Tracing (Correlation ID) | **AWS Lambda + Amazon S3 (Modo Object Lock)** | • Memoria: 256 MB.<br>• Timeout: 5 segundos.<br>• Inmutabilidad forzada a nivel de motor mediante política S3 Object Lock (bloqueo total de UPDATE/DELETE). |
| **Servicio Multimedia** | • Decouple Persistence (URLs presignadas)<br>• Scale-to-zero | **AWS Lambda + Amazon S3 (Object Storage)** | • Memoria: 512 MB.<br>• Timeout: 15 segundos.<br>• URL prefirmada de un solo uso para cargas.<br>• Almacenamiento con Storage Tiering (migración automática a S3 Glacier después de 30 días para ahorro del 30% de costos). |
| **Persistencia SQL Core** *(Usuarios, Reportes, Obras)* | • Active Redundancy<br>• Automated Failover<br>• Backups periódicos | **Amazon RDS para PostgreSQL (Multi-AZ)** | • Instancia de base de datos dedicada tipo `db.m5.large`.<br>• Replicación activa en caliente síncrona en Multi-AZ.<br>• Métricas de recuperación: RTO < 1 hora y RPO < 15 minutos.<br>• Cifrado en reposo vía AWS KMS. |
| **Servicio y Persistencia Geoespacial** | • Bulkhead Isolation (Pool de conexiones aislado)<br>• Horizontal Partitioning (R-Tree/Bounding Box)<br>• Caching | **Amazon RDS para PostgreSQL + Extensión PostGIS** *(Instancia independiente)* | • Pool de conexiones aislado y acotado a un máximo de 50 conexiones dedicadas.<br>• Índice espacial GiST/R-Tree activo para optimización.<br>• Tiempo de respuesta objetivo para consultas a mapas < 200 ms. |
| **Persistencia In-Memory** | • Caching distribuido<br>• Control de Idempotencia distribuido<br>• Control de tasa compartido | **Amazon ElastiCache para Redis** | • Acceso restringido estrictamente a la VPC interna.<br>• TTL de llaves de idempotencia fijado a 24 horas.<br>• TTL de sesión sincronizado con la expiración del JWT. |
| **Bus de Eventos (Event Bus)** | • Asynchronous Decoupling<br>• Event Streaming<br>• Saga Coreografiada | **Amazon MQ (Broker RabbitMQ gestionado)** | • Clúster de estándar abierto (AMQP) compuesto por 3 nodos con réplica de colas activas.<br>• Políticas de recursos estrictas que restringen qué servicios pueden publicar o suscribirse según el tipo de evento de dominio. |
| **Dead Letter Queue (DLQ)** | • Dead Letter Channel<br>• Dead Letter Enqueueing | **Amazon MQ (Cola de mensajes muertos integrada)** | • Desvío automático a la DLQ compartida tras agotar un límite estricto de 3 intentos de procesamiento.<br>• Retención obligatoria de mensajes fallidos por 14 días para auditorías. |

---

## 2. ANÁLISIS DE COMPENSACIONES (*TRADE-OFFS*) Y CONSECUENCIAS ARQUITECTÓNICAS

### Tensión Seguridad vs. Rendimiento (QAS-01.1 & QAS-01.2)
* **El Conflicto:** La incorporación de políticas estrictas de autenticación perimetral (validación de firmas criptográficas de tokens JWT RS256), control de acceso basado en roles (RBAC) y limitación de tasa por dirección IP (*Token Bucket* a 100 req/s), sumado a la validación e inspección sanea de esquemas para mitigar inyecciones SQL, añade un procesamiento extra inevitable en el camino crítico de cada solicitud HTTP.
* **El Trade-off:** Se prioriza la integridad de los datos de la ciudadanía y la seguridad del Estado por encima de una latencia mínima absoluta en el borde perimetral.
* **Tratamiento de Mitigación:** Todo el filtrado de seguridad dura y el control de tráfico se centralizan en el **API Gateway + WAF**, bloqueando peticiones maliciosas o abusivas en menos de 5 ms. Esto garantiza que las solicitudes inválidas sean rechazadas en el perímetro de la red y nunca alcancen ni consuman ciclos de cómputo o memoria en las bases de datos o servicios de negocio del Core.

### Tensión Disponibilidad vs. Costo (QAS-06 & ADR-05)
* **El Conflicto:** Para cumplir con los objetivos de resiliencia del núcleo transaccional (Usuarios, Reportes y Obras) ante desastres físicos o caídas de infraestructura, la arquitectura exige una estrategia de **Redundancia Activa** mediante una instancia réplica en caliente sincronizada por red (*Streaming Replication*) con conmutación por error automática (*Automated Failover*). Esto rompe la disciplina de "escala a cero" utilizada en el cómputo y añade un costo financiero permanente e inactivo el 99% del tiempo de operación regular.
* **El Trade-off:** El equipo evaluó una alternativa económica basada únicamente en respaldos periódicos (*backups* en frío), la cual reduciría a cero el costo de la réplica. Sin embargo, se descartó de forma consciente debido a que el Tiempo de Recuperación (RTO) se dispararía a varias horas durante el peor escenario operativo imaginable: una emergencia climática regional donde el sistema es más crítico para los ciudadanos.
* **Tratamiento de Mitigación:** Se asume el costo fijo permanente de la infraestructura de base de datos (`db.m5.large` Multi-AZ). Esta inversión se compensa financieramente gracias al **ADR-03**, el cual implementa *Serverless Containers* y funciones *FaaS* que escalan a cero absoluto durante periodos de inactividad, logrando una reducción drástica en el costo operativo total (OPEX) de la capa de cómputo.

### Tensión Resiliencia vs. Inmediatez (QAS-02 & ADR-01)
* **El Conflicto:** Aislar el ecosistema de las caídas intermitentes o degradación de latencia de proveedores externos (como SendGrid para correos o FCM para notificaciones *push*) requiere romper la comunicación síncrona inmediata. Al encapsular las llamadas de red externas con un *Circuit Breaker* y delegar el flujo a una cola asíncrona de reintentos con *Backoff* exponencial y desvío a *Dead Letter Queue* (DLQ), las alertas al ciudadano dejan de ser instantáneas bajo condiciones de falla generalizada.
* **El Trade-off:** Se sacrifica la inmediatez de la notificación visual en favor de asegurar que ningún mensaje sea destruido o descartado en el camino, resguardando la consistencia transaccional.
* **Tratamiento de Mitigación:** Al abrirse el circuito por fallas continuas en la dependencia externa, el sistema entra en modo de **Fallo Rápido (< 50 ms)** y ejecuta un mecanismo de contingencia (*Fallback*). El mensaje se retiene de forma segura en background dentro de la DLQ en el bus de eventos AMQP para ser reprocesado automáticamente una vez que el proveedor se estabilice.

---

## 3. DIAGRAMAS CONCEPTUALES DE FLUJOS DETALLADOS

### Flujo de la Saga Coreografiada asíncrona sobre AMQP (ADR-01)

```
[ Ciudadano / App ]       [ Servicio Reportes ]       [ Broker RabbitMQ ]       [ FaaS Auditoría ]     [ FaaS Notificaciones ]
        |                         |                            |                        |                         |
        |--- 1. POST Reporte ---->|                            |                        |                         |
        |                         |--- 2. Persiste Local ACID->|                        |                         |
        |<-- 3. HTTP 201 (<150ms)-|                            |                        |                         |
        |    (Reporte Creado)     |--- 4. Publica Evento ---->|                        |                         |
        |                         |    "reporte.creado"        |                        |                         |
        |                         |                            |--- 5. Distribuye ----->|                         |
        |                         |                            |    (Topic: q_audit)    |--- 6. Registra bloque ->|
        |                         |                            |                        |    Append-Only + Hash   |
        |                         |                            |                        |    Chaining (Inmutable) |
        |                         |                            |                        |                         |
        |                         |                            |--- 7. Distribuye ------------------------------->|
        |                         |                            |    (Topic: q_notif)    |                         |--- 8. Despacha Alerta
        |                         |                            |                        |                         |    Push / SMS Externa
```

### Flujo de Resiliencia del Circuit Breaker, Fallback y DLQ (ADR-02)

```
[ Broker RabbitMQ ]     [ FaaS Notificaciones ]     [ Circuit Breaker ]     [ Proveedor Externo ]     [ Dead Letter Queue ]
        |                         |                          |                       |                        |
        |--- 1. Entrega Mensaje ->|                          |                       |                        |
        |                         |--- 2. Invocar Endpoint ->|                       |                        |
        |                         |                          |--- 3. HTTP POST ---->|                        |
        |                         |                          |    (Timeout / 503)    |                        |
        |                         |                          |<- 4. Retorna Error ---|                        |
        |                         |                          |                       |                        |
        |                         |                          | [Evaluación Interna:  |                        |
        |                         |                          |  ¿Fallos Consec >= 3? |                        |
        |                         |                          |  SÍ -> Estado: OPEN ] |                        |
        |                         |                          |                       |                        |
        |                         |<-- 5. Intercepta / OPEN -|                       |                        |
        |                         |    (Fallo Rápido < 50ms) |                       |                        |
        |                         |                          |                       |                        |
        |                         |--- 6. Ejecuta Fallback (basic_reject) ----------------------------------->|
        |                         |                                                                           |-- 7. Almacena mensaje
        |                         |                                                                               para auditoría /
        |                         |                                                                               reproceso manual
```
