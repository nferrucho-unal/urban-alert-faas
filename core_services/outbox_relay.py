import json
import logging
import os
import time

import pika

from core_services.common import database_connection

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
LOGGER = logging.getLogger("outbox-relay")
BROKER_URL = os.environ.get(
    "BROKER_URL", "amqp://urban_user:urban_secure_pass@localhost:5672/%2F"
)
EXCHANGE = "urban_alert_events"
BATCH_SIZE = int(os.environ.get("OUTBOX_BATCH_SIZE", "50"))
POLL_INTERVAL = float(os.environ.get("OUTBOX_POLL_INTERVAL", "0.25"))


def publish_pending(channel):
    connection = database_connection()
    try:
        with connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT id, tipo, payload
                    FROM outbox_eventos
                    WHERE publicado_en IS NULL
                    ORDER BY creado_en, id
                    FOR UPDATE SKIP LOCKED
                    LIMIT %s
                    """,
                    (BATCH_SIZE,),
                )
                events = cursor.fetchall()
                for row in events:
                    event_id = row["id"]
                    event_type = row["tipo"]
                    payload = row["payload"]
                    if not isinstance(payload, dict):
                        payload = json.loads(payload)
                    properties = pika.BasicProperties(
                        content_type="application/json",
                        delivery_mode=2,
                        correlation_id=payload["correlationId"],
                        message_id=str(event_id),
                        type=event_type,
                    )
                    channel.basic_publish(
                        exchange=EXCHANGE,
                        routing_key=event_type,
                        body=json.dumps(payload, separators=(",", ":")),
                        properties=properties,
                        mandatory=True,
                    )
                    cursor.execute(
                        "UPDATE outbox_eventos SET publicado_en = now() WHERE id = %s",
                        (event_id,),
                    )
        return len(events)
    finally:
        connection.close()


def run_relay():
    while True:
        broker = None
        try:
            broker = pika.BlockingConnection(pika.URLParameters(BROKER_URL))
            channel = broker.channel()
            channel.exchange_declare(exchange=EXCHANGE, exchange_type="topic", durable=True)
            channel.confirm_delivery()
            LOGGER.info("Relay outbox conectado a RabbitMQ")
            while True:
                count = publish_pending(channel)
                if count == 0:
                    time.sleep(POLL_INTERVAL)
        except Exception:
            LOGGER.exception("Relay outbox falló; los mensajes pendientes se reintentarán")
            time.sleep(2)
        finally:
            if broker and broker.is_open:
                broker.close()


if __name__ == "__main__":
    run_relay()