import json
import logging
import os
import sys
import time

import pika
import redis
from psycopg2.extras import RealDictCursor

from core_services.common import database_connection

from validar.event_contracts import (
    ContractValidationError,
    UnsupportedEventError,
    dead_letter_invalid_message,
    validate_event,
)

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
LOGGER = logging.getLogger("geospatial-projector")
BROKER_URL = os.environ.get(
    "BROKER_URL", "amqp://urban_user:urban_secure_pass@localhost:5672/%2F"
)
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
SUPPORTED_EVENTS = {"reporte.creado", "reporte.validado"}
DLQ_EXCHANGE = "urban_alert_dlx"
DLQ_ROUTING_KEY = "geospatial.invalid"


def project_event(event):
    report_id = event["reportId"]
    data = event["data"]
    connection = database_connection(geo=True)
    try:
        with connection:
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                if event["eventType"] == "reporte.creado":
                    cursor.execute(
                        """
                        INSERT INTO reportes_geo
                            (reporte_id, municipio_id, categoria, estado, geom, creado_en)
                        VALUES (%s, %s, %s, 'RECIBIDO',
                                ST_SetSRID(ST_MakePoint(%s, %s), 4326), %s)
                        ON CONFLICT (reporte_id) DO UPDATE SET
                            municipio_id = EXCLUDED.municipio_id,
                            categoria = EXCLUDED.categoria,
                            estado = EXCLUDED.estado,
                            geom = EXCLUDED.geom
                        """,
                        (
                            report_id,
                            data["municipioId"],
                            data["categoria"],
                            data["lon"],
                            data["lat"],
                            event["occurredAt"],
                        ),
                    )
                elif event["eventType"] == "reporte.validado":
                    cursor.execute(
                        "UPDATE reportes_geo SET estado = 'VALIDADO' WHERE reporte_id = %s",
                        (report_id,),
                    )
                    if cursor.rowcount == 0:
                        raise RuntimeError("La proyección aún no contiene el reporte creado.")
    finally:
        connection.close()


def callback_projector(ch, method, properties, body):
    try:
        event = validate_event(json.loads(body.decode("utf-8")))
    except UnsupportedEventError as error:
        LOGGER.warning("Evento geoespacial ignorado: %s", error)
        ch.basic_ack(delivery_tag=method.delivery_tag)
        return
    except (UnicodeDecodeError, json.JSONDecodeError, ContractValidationError) as error:
        LOGGER.error("Contrato geoespacial inválido: %s", error)
        dead_letter_invalid_message(
            ch, method, properties, body, DLQ_ROUTING_KEY, str(error)
        )
        return

    event_id = event["eventId"]
    if event["eventType"] not in SUPPORTED_EVENTS:
        LOGGER.warning("Evento no proyectado: %s", event["eventType"])
        ch.basic_ack(delivery_tag=method.delivery_tag)
        return

    redis_client = redis.Redis.from_url(REDIS_URL, decode_responses=True)
    key = f"geospatial:projection:{event_id}"
    try:
        if redis_client.exists(key):
            ch.basic_ack(delivery_tag=method.delivery_tag)
            return
        project_event(event)
        redis_client.set(key, "1", ex=24 * 60 * 60)
        redis_client.delete(f"geospatial:attempt:{event_id}")
        ch.basic_ack(delivery_tag=method.delivery_tag)
    except Exception as error:
        LOGGER.exception("Falló proyección eventId=%s", event_id)
        attempt_key = f"geospatial:attempt:{event_id}"
        attempts = redis_client.incr(attempt_key)
        redis_client.expire(attempt_key, 24 * 60 * 60)
        if attempts >= 5:
            dead_letter_invalid_message(
                ch,
                method,
                properties,
                body,
                "geospatial.processing_failed",
                str(error),
            )
        else:
            ch.basic_nack(delivery_tag=method.delivery_tag, requeue=True)


def iniciar_consumidor():
    redis_client = redis.Redis.from_url(REDIS_URL, decode_responses=True)
    redis_client.ping()
    connection = pika.BlockingConnection(pika.URLParameters(BROKER_URL))
    channel = connection.channel()
    channel.basic_qos(prefetch_count=1)
    channel.exchange_declare(exchange="urban_alert_events", exchange_type="topic", durable=True)
    channel.exchange_declare(exchange=DLQ_EXCHANGE, exchange_type="topic", durable=True)
    channel.queue_declare(queue="q_dead_letter_geospatial", durable=True)
    channel.queue_bind(
        exchange=DLQ_EXCHANGE,
        queue="q_dead_letter_geospatial",
        routing_key=DLQ_ROUTING_KEY,
    )
    channel.queue_bind(
        exchange=DLQ_EXCHANGE,
        queue="q_dead_letter_geospatial",
        routing_key="geospatial.processing_failed",
    )
    channel.queue_declare(queue="q_geospatial_projection", durable=True)
    for event_type in SUPPORTED_EVENTS:
        channel.queue_bind(
            exchange="urban_alert_events",
            queue="q_geospatial_projection",
            routing_key=event_type,
        )
    channel.confirm_delivery()
    channel.basic_consume(queue="q_geospatial_projection", on_message_callback=callback_projector)
    LOGGER.info("Proyector geoespacial escuchando eventos %s", sorted(SUPPORTED_EVENTS))
    channel.start_consuming()


if __name__ == "__main__":
    while True:
        try:
            iniciar_consumidor()
        except Exception:
            LOGGER.exception("Consumidor geoespacial detenido; reintentando conexión")
            time.sleep(2)