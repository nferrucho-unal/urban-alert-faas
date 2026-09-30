import hashlib
import json
import os
import sys
import pika
from validar.event_contracts import (
    ContractValidationError,
    UnsupportedEventError,
    dead_letter_invalid_message,
    validate_event,
)

BROKER_URL = os.environ.get(
    "BROKER_URL", "amqp://urban_user:urban_secure_pass@localhost:5672/%2F"
)
EVENT_STORE = []  # Bitácora Append-only simulada en memoria
EVENT_IDS = set()
AUDIT_EVENT_TYPES = {
    "reporte.creado",
    "reporte.validado",
    "obra.asignada",
    "usuario.rol_cambiado",
}


def calcular_hash_evento(evento_dict):
    evento_string = json.dumps(evento_dict, sort_keys=True)
    return hashlib.sha256(evento_string.encode("utf-8")).hexdigest()


def callback_auditoria(ch, method, properties, body):
    """Valida eventos de dominio compatibles antes de registrar la auditoría."""
    try:
        evento_origen = validate_event(json.loads(body.decode("utf-8")))
    except UnsupportedEventError as error:
        print(f"Evento de auditoría ignorado: {error}", file=sys.stderr, flush=True)
        ch.basic_ack(delivery_tag=method.delivery_tag)
        return
    except (UnicodeDecodeError, json.JSONDecodeError, ContractValidationError) as error:
        print(f"Evento de auditoría inválido: {error}", file=sys.stderr, flush=True)
        dead_letter_invalid_message(
            ch, method, properties, body, "auditoria.invalid", str(error)
        )
        return

    if evento_origen["eventType"] not in AUDIT_EVENT_TYPES:
        print(
            f"Evento no consumido por auditoría: {evento_origen['eventType']}",
            file=sys.stderr,
            flush=True,
        )
        ch.basic_ack(delivery_tag=method.delivery_tag)
        return

    try:
        if evento_origen["eventId"] in EVENT_IDS:
            ch.basic_ack(delivery_tag=method.delivery_tag)
            return

        # Estructurar bloque de auditoría inmutable (Hash Chaining)
        hash_previo = (
            calcular_hash_evento(EVENT_STORE[-1])
            if EVENT_STORE
            else "00000000"
        )
        nuevo_bloque = {
            "eventId": evento_origen["eventId"],
            "reportId": evento_origen["reportId"],
            "evento": evento_origen["eventType"],
            "actorId": evento_origen["data"]["actorId"],
            "correlationId": evento_origen["correlationId"],
            "occurredAt": evento_origen["occurredAt"],
            "hash_anterior": hash_previo,
        }

        EVENT_STORE.append(nuevo_bloque)
        EVENT_IDS.add(evento_origen["eventId"])

        # Structured Logging obligatorio (Factor XI - stdout)
        print(
            json.dumps({"log_level": "INFO", "audit_record": nuevo_bloque}),
            flush=True,
        )

        # Confirmar procesamiento exitoso a RabbitMQ (Ack)
        ch.basic_ack(delivery_tag=method.delivery_tag)
    except Exception as e:
        print(f"Error procesando auditoría: {e}", file=sys.stderr, flush=True)
        ch.basic_nack(delivery_tag=method.delivery_tag, requeue=True)


def iniciar_consumidor():
    # Establecer la conexión con el contenedor RabbitMQ
    params = pika.URLParameters(BROKER_URL)
    connection = pika.BlockingConnection(params)
    channel = connection.channel()
    channel.basic_qos(prefetch_count=1)

    # Declarar e interconectar Exchange y Cola bajo el patrón Publish-Subscribe (Pág 13)
    channel.exchange_declare(exchange="urban_alert_events", exchange_type="topic", durable=True)
    channel.exchange_declare(exchange="urban_alert_dlx", exchange_type="topic", durable=True)
    channel.queue_declare(queue="q_dead_letter_audit", durable=True)
    channel.queue_bind(
        exchange="urban_alert_dlx",
        queue="q_dead_letter_audit",
        routing_key="auditoria.invalid",
    )
    channel.queue_declare(queue="q_auditoria_core", durable=True)
    for event_type in AUDIT_EVENT_TYPES:
        channel.queue_bind(
            exchange="urban_alert_events",
            queue="q_auditoria_core",
            routing_key=event_type,
        )

    print(
        " [*] FaaS Auditoría esperando eventos de dominio en 'q_auditoria_core'.",
        flush=True,
    )
    channel.basic_consume(
        queue="q_auditoria_core", on_message_callback=callback_auditoria
    )
    channel.confirm_delivery()
    channel.start_consuming()


if __name__ == "__main__":
    iniciar_consumidor()
