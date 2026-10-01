import hashlib
import json
import os
import time
import uuid

import pika
import psycopg2
import redis
import requests
from eventos import evento_reporte_creado

BROKER_URL = os.environ.get(
    "BROKER_URL", "amqp://urban_user:urban_secure_pass@localhost:5672/%2F"
)
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
CORE_DATABASE_URL = os.environ.get(
    "CORE_DATABASE_URL",
    "postgresql://core_admin:core_secure_pass@localhost:5431/urban_alert_core",
)
MAILPIT_API_URL = os.environ.get(
    "MAILPIT_API_URL", "http://localhost:8025/api/v1"
)
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
            if record.get("status") in {"SENT", "FAILED", "SKIPPED"}:
                return record
        time.sleep(0.25)
    raise AssertionError(f"No apareció estado terminal en Redis para {key}")


def preparar_destinatario_y_reporte(event):
    data = event["data"]
    email = f"{event['eventId']}@example.test"
    with psycopg2.connect(CORE_DATABASE_URL) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO usuarios
                    (username, rol, cognito_sub, email, display_name, municipio_id, activo)
                VALUES (%s, 'ciudadano', %s, %s, 'Mailpit Test', %s, TRUE)
                ON CONFLICT (cognito_sub) DO UPDATE SET
                    email = EXCLUDED.email,
                    display_name = EXCLUDED.display_name,
                    activo = TRUE
                """,
                (
                    data["actorId"],
                    data["actorId"],
                    email,
                    data["municipioId"],
                ),
            )
            cursor.execute(
                """
                INSERT INTO reportes_core
                    (report_id, descripcion, estado, categoria, actor_id,
                     municipio_id, lat, lon, correlation_id)
                VALUES (%s, %s, 'RECIBIDO', %s, %s, %s, %s, %s, %s)
                """,
                (
                    event["reportId"],
                    "Reporte de prueba de notificación SMTP",
                    data["categoria"],
                    data["actorId"],
                    data["municipioId"],
                    data["lat"],
                    data["lon"],
                    event["correlationId"],
                ),
            )
    return email


def esperar_correo(event, recipient_email):
    expected_message_id = f"{event['eventId']}@urban-alert"
    deadline = time.monotonic() + POLL_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        response = requests.get(f"{MAILPIT_API_URL}/messages", timeout=3)
        response.raise_for_status()
        for message in response.json().get("messages", []):
            if message.get("MessageID", "").strip("<>") != expected_message_id:
                continue
            detail = requests.get(
                f"{MAILPIT_API_URL}/message/{message['ID']}", timeout=3
            )
            detail.raise_for_status()
            email = detail.json()
            recipients = {item["Address"] for item in email.get("To", [])}
            assert recipient_email in recipients, email
            assert email["Subject"] == "Urban Alert: Reporte recibido", email
            assert event["reportId"] in email.get("Text", ""), email
            return email
        time.sleep(0.25)
    raise AssertionError(f"Mailpit no recibió el correo para {recipient_email}")


def probar_idempotencia_y_estado_redis():
    client = redis.Redis.from_url(REDIS_URL, decode_responses=True)
    client.ping()

    event_id = str(uuid.uuid4())
    event = evento_reporte_creado(event_id=event_id)
    report_id = event["reportId"]
    recipient_email = preparar_destinatario_y_reporte(event)
    body = json.dumps(event, separators=(",", ":")).encode("utf-8")
    payload_hash = hashlib.sha256(body).hexdigest()
    key = f"notification:{event_id}"

    publicar_evento(event["correlationId"], body)
    record = esperar_estado(client, key)
    assert record["report_id"] == report_id, record
    assert record["payload_hash"] == payload_hash, record
    assert record["status"] == "SENT", record
    assert record["attempts"] in {1, 2, 3}, record
    assert client.ttl(key) > 0
    email = esperar_correo(event, recipient_email)

    publicar_evento(event["correlationId"], body)
    time.sleep(0.5)
    duplicate_record = json.loads(client.get(key))
    assert duplicate_record == record, duplicate_record

    with psycopg2.connect(CORE_DATABASE_URL) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM reportes_core WHERE report_id = %s", (report_id,)
            )
            cursor.execute(
                "DELETE FROM usuarios WHERE cognito_sub = %s",
                (event["data"]["actorId"],),
            )

    print(
        f"[OK] Email {email['Subject']} enviado a {recipient_email}; "
        f"estado Redis {record['status']}, TTL={client.ttl(key)}s; "
        "el reenvío conserva la idempotencia."
    )


if __name__ == "__main__":
    probar_idempotencia_y_estado_redis()
