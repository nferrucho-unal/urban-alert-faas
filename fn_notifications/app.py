import hashlib
import json
import os
import random
import sys
import time

import pika
import redis
from validar.event_contracts import (
    ContractValidationError,
    UnsupportedEventError,
    validate_event,
)

BROKER_URL = os.environ.get(
    "BROKER_URL", "amqp://urban_user:urban_secure_pass@localhost:5672/%2F"
)
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
IDEMPOTENCY_TTL_SECONDS = 24 * 60 * 60
PROCESSING_TTL_SECONDS = 30
MAX_PROVIDER_ATTEMPTS = 3
CIRCUIT_FAILURE_THRESHOLD = 3
CIRCUIT_COOLDOWN_SECONDS = 4
BASE_RETRY_DELAY_SECONDS = 0.2
NOTIFICATION_EVENT_TYPES = {"reporte.creado", "obra.asignada"}

redis_client = redis.Redis.from_url(REDIS_URL, decode_responses=True)
provider_failure_count = 0
circuit_open_until = 0.0


class ProviderUnavailableError(Exception):
    pass


def clave_idempotencia(event_id):
    return f"notification:{event_id}"


def guardar_estado(redis_key, event_id, payload_hash, report_id, status, **extra):
    record = {
        "event_id": event_id,
        "payload_hash": payload_hash,
        "report_id": report_id,
        "status": status,
        **extra,
    }
    ttl = PROCESSING_TTL_SECONDS if status == "PROCESSING" else IDEMPOTENCY_TTL_SECONDS
    redis_client.set(redis_key, json.dumps(record), ex=ttl)


def enviar_notificacion():
    """Simula el proveedor externo local; sustituir por SES/SNS/FCM en cloud."""
    global circuit_open_until, provider_failure_count
    if time.monotonic() < circuit_open_until:
        raise ProviderUnavailableError("Circuit breaker abierto")

    if random.random() < 0.4:
        provider_failure_count += 1
        if provider_failure_count >= CIRCUIT_FAILURE_THRESHOLD:
            circuit_open_until = time.monotonic() + CIRCUIT_COOLDOWN_SECONDS
        raise ProviderUnavailableError("Proveedor externo inactivo (fallo simulado)")

    provider_failure_count = 0


def callback_notificaciones_con_dlq(ch, method, properties, body):
    """Valida reporte.creado, aplica idempotencia Redis y preserva la DLQ."""
    try:
        evento = validate_event(json.loads(body.decode("utf-8")))
    except UnsupportedEventError as error:
        print(f"Evento de notificación ignorado: {error}", file=sys.stderr, flush=True)
        ch.basic_ack(delivery_tag=method.delivery_tag)
        return
    except (UnicodeDecodeError, json.JSONDecodeError, ContractValidationError) as error:
        print(f"Evento de notificación inválido: {error}", file=sys.stderr, flush=True)
        ch.basic_reject(delivery_tag=method.delivery_tag, requeue=False)
        return

    if evento["eventType"] not in NOTIFICATION_EVENT_TYPES:
        print(
            f"Evento no consumido por notificaciones: {evento['eventType']}",
            file=sys.stderr,
            flush=True,
        )
        ch.basic_ack(delivery_tag=method.delivery_tag)
        return

    report_id = evento["reportId"]
    payload_hash = hashlib.sha256(body).hexdigest()
    event_id = evento["eventId"]
    redis_key = clave_idempotencia(event_id)

    try:
        existing_raw = redis_client.get(redis_key)
        if existing_raw:
            existing = json.loads(existing_raw)
            if existing.get("payload_hash") != payload_hash:
                print(
                    f"Payload alterado para correlation_id={event_id}; enviando a DLQ.",
                    file=sys.stderr,
                    flush=True,
                )
                ch.basic_reject(delivery_tag=method.delivery_tag, requeue=False)
                return
            if existing.get("status") in {"SENT", "FAILED"}:
                print(f" [*] Evento duplicado omitido: {event_id}.", flush=True)
                ch.basic_ack(delivery_tag=method.delivery_tag)
                return
            ch.basic_nack(delivery_tag=method.delivery_tag, requeue=True)
            return

        reserved = redis_client.set(
            redis_key,
            json.dumps(
                {
                    "event_id": event_id,
                    "payload_hash": payload_hash,
                    "report_id": report_id,
                    "status": "PROCESSING",
                }
            ),
            nx=True,
            ex=PROCESSING_TTL_SECONDS,
        )
        if not reserved:
            ch.basic_nack(delivery_tag=method.delivery_tag, requeue=True)
            return

        last_error = None
        for attempt in range(1, MAX_PROVIDER_ATTEMPTS + 1):
            try:
                enviar_notificacion()
                guardar_estado(
                    redis_key,
                    event_id,
                    payload_hash,
                    report_id,
                    "SENT",
                    attempts=attempt,
                )
                print(
                    f" [ÉXITO] Alerta despachada y guardada en Redis para {report_id}.",
                    flush=True,
                )
                ch.basic_ack(delivery_tag=method.delivery_tag)
                return
            except ProviderUnavailableError as error:
                last_error = str(error)
                if attempt < MAX_PROVIDER_ATTEMPTS:
                    delay = BASE_RETRY_DELAY_SECONDS * (2 ** (attempt - 1))
                    time.sleep(random.uniform(0, delay))

        guardar_estado(
            redis_key,
            event_id,
            payload_hash,
            report_id,
            "FAILED",
            attempts=MAX_PROVIDER_ATTEMPTS,
            error=last_error,
        )
        print(
            f" [FALLO] {last_error}; agotados los reintentos, enviando a DLQ.",
            flush=True,
        )
        ch.basic_reject(delivery_tag=method.delivery_tag, requeue=False)
    except redis.RedisError as error:
        print(f"Error de idempotencia Redis: {error}", file=sys.stderr, flush=True)
        ch.basic_nack(delivery_tag=method.delivery_tag, requeue=True)
    except Exception as error:
        print(f"Error procesando notificación: {error}", file=sys.stderr, flush=True)
        ch.basic_nack(delivery_tag=method.delivery_tag, requeue=True)

def iniciar_consumidor():
    redis_client.ping()
    print(" [*] Redis disponible para idempotencia (TTL 24h).", flush=True)
    params = pika.URLParameters(BROKER_URL)
    connection = pika.BlockingConnection(params)
    channel = connection.channel()

    channel.basic_qos(prefetch_count=1)
    channel.exchange_declare(exchange="urban_alert_dlx", exchange_type="topic", durable=True)
    channel.queue_declare(queue="q_dead_letter_notifications", durable=True)
    channel.queue_bind(
        exchange="urban_alert_dlx",
        queue="q_dead_letter_notifications",
        routing_key="notificaciones.failed",
    )

    channel.exchange_declare(exchange="urban_alert_events", exchange_type="topic", durable=True)
    channel.queue_declare(
        queue="q_notificaciones_core",
        durable=True,
        arguments={
            "x-dead-letter-exchange": "urban_alert_dlx",
            "x-dead-letter-routing-key": "notificaciones.failed",
        },
    )
    for event_type in NOTIFICATION_EVENT_TYPES:
        channel.queue_bind(
            exchange="urban_alert_events",
            queue="q_notificaciones_core",
            routing_key=event_type,
        )

    print(" [*] FaaS Notificaciones escuchando ReporteCreado con DLQ activa.", flush=True)
    channel.basic_consume(
        queue="q_notificaciones_core",
        on_message_callback=callback_notificaciones_con_dlq,
    )
    channel.start_consuming()

if __name__ == "__main__":
    iniciar_consumidor()
