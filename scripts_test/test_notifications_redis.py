import hashlib
import json
import os
import time
import uuid

import pika
import redis
from eventos import evento_reporte_creado

BROKER_URL = os.environ.get(
    "BROKER_URL", "amqp://urban_user:urban_secure_pass@localhost:5672/%2F"
)
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
POLL_TIMEOUT_SECONDS = 30


def publicar_evento(event_id, body):
    connection = pika.BlockingConnection(pika.URLParameters(BROKER_URL))
    try:
        channel = connection.channel()
        channel.exchange_declare(exchange="urban_alert_events", exchange_type="topic", durable=True)
        channel.confirm_delivery()
        channel.basic_publish(
            exchange="urban_alert_events",
            routing_key="reporte.creado",
            body=body,
            properties=pika.BasicProperties(
                correlation_id=event_id,
                content_type="application/json",
            ),
            mandatory=True,
        )
    finally:
        connection.close()


def esperar_estado(client, key):
    deadline = time.monotonic() + POLL_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        value = client.get(key)
        if value:
            record = json.loads(value)
            if record.get("status") in {"SENT", "FAILED"}:
                return record
        time.sleep(0.25)
    raise AssertionError(f"No apareció estado terminal en Redis para {key}")


def probar_idempotencia_y_estado_redis():
    client = redis.Redis.from_url(REDIS_URL, decode_responses=True)
    client.ping()

    event_id = str(uuid.uuid4())
    event = evento_reporte_creado(event_id=event_id)
    report_id = event["reportId"]
    body = json.dumps(event, separators=(",", ":")).encode("utf-8")
    payload_hash = hashlib.sha256(body).hexdigest()
    key = f"notification:{event_id}"

    publicar_evento(event["correlationId"], body)
    record = esperar_estado(client, key)
    assert record["report_id"] == report_id, record
    assert record["payload_hash"] == payload_hash, record
    assert record["status"] in {"SENT", "FAILED"}, record
    assert record["attempts"] in {1, 2, 3}, record
    assert client.ttl(key) > 0

    publicar_evento(event["correlationId"], body)
    time.sleep(0.5)
    duplicate_record = json.loads(client.get(key))
    assert duplicate_record == record, duplicate_record

    print(
        f"[OK] Estado Redis {record['status']}, TTL={client.ttl(key)}s; "
        "reenvío del mismo evento conserva el registro idempotente."
    )


if __name__ == "__main__":
    probar_idempotencia_y_estado_redis()
