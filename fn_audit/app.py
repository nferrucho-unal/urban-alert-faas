import hashlib
import json
import os
import sys
import pika

BROKER_URL = os.environ.get(
    "BROKER_URL", "amqp://urban_user:urban_secure_pass@localhost:5672/%2F"
)
EVENT_STORE = []  # Bitácora Append-only simulada en memoria


def calcular_hash_evento(evento_dict):
    evento_string = json.dumps(evento_dict, sort_keys=True)
    return hashlib.sha256(evento_string.encode("utf-8")).hexdigest()


def callback_auditoria(ch, method, properties, body):
    """Manejador FaaS que reacciona de manera asíncrona ante 'ReporteCreado' (QAS-04.1)."""
    try:
        evento_origen = json.loads(body.decode("utf-8"))

        # Estructurar bloque de auditoría inmutable (Hash Chaining)
        hash_previo = (
            calcular_hash_evento(EVENT_STORE[-1])
            if EVENT_STORE
            else "00000000"
        )
        nuevo_bloque = {
            "reportId": evento_origen.get("reportId"),
            "evento": "ReporteCreado",
            "actor": evento_origen.get("actor"),
            "correlationId": properties.correlation_id,
            "hash_anterior": hash_previo,
        }

        EVENT_STORE.append(nuevo_bloque)

        # Structured Logging obligatorio (Factor XI - stdout)
        print(
            json.dumps({"log_level": "INFO", "audit_record": nuevo_bloque}),
            flush=True,
        )

        # Confirmar procesamiento exitoso a RabbitMQ (Ack)
        ch.basic_ack(delivery_tag=method.delivery_tag)
    except Exception as e:
        print(f"Error procesando auditoría: {e}", file=sys.stderr)


def iniciar_consumidor():
    # Establecer la conexión con el contenedor RabbitMQ
    params = pika.URLParameters(BROKER_URL)
    connection = pika.BlockingConnection(params)
    channel = connection.channel()

    # Declarar e interconectar Exchange y Cola bajo el patrón Publish-Subscribe (Pág 13)
    channel.exchange_declare(exchange="urban_alert_events", exchange_type="topic")
    channel.queue_declare(queue="q_auditoria_core", durable=True)
    channel.queue_bind(
        exchange="urban_alert_events",
        queue="q_auditoria_core",
        routing_key="reporte.creado",
    )

    print(
        " [*] FaaS Auditoría esperando eventos en 'q_auditoria_core'.",
        flush=True,
    )
    channel.basic_consume(
        queue="q_auditoria_core", on_message_callback=callback_auditoria
    )
    channel.start_consuming()


if __name__ == "__main__":
    iniciar_consumidor()
