import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone

import boto3
import pika
import psycopg2
from botocore.exceptions import ClientError
from psycopg2.extras import Json
from core_services.audit_integrity import calcular_hash_evento

from validar.event_contracts import (
    ContractValidationError,
    UnsupportedEventError,
    dead_letter_invalid_message,
    validate_event,
)

BROKER_URL = os.environ.get(
    "BROKER_URL", "amqp://urban_user:urban_secure_pass@localhost:5672/%2F"
)
AUDIT_DATABASE_URL = os.environ.get("AUDIT_DATABASE_URL")
AUDIT_ARCHIVE_BUCKET = os.environ.get("AUDIT_ARCHIVE_BUCKET")
AUDIT_OBJECT_LOCK_RETENTION_DAYS = int(
    os.environ.get("AUDIT_OBJECT_LOCK_RETENTION_DAYS", "2555")
)
LOGGER = logging.getLogger("urban-alert-audit")
AUDIT_EVENT_TYPES = {
    "reporte.creado",
    "reporte.validado",
    "reporte.rechazado",
    "reporte.resuelto",
    "obra.asignada",
    "usuario.rol_cambiado",
}


def database_connection():
    if not AUDIT_DATABASE_URL:
        raise RuntimeError("Falta configurar AUDIT_DATABASE_URL.")
    return psycopg2.connect(AUDIT_DATABASE_URL)


def _audit_record(evento, hash_previo, record_hash):
    return {
        "eventId": evento["eventId"],
        "reportId": evento["reportId"],
        "evento": evento["eventType"],
        "actorId": evento["data"]["actorId"],
        "correlationId": evento["correlationId"],
        "occurredAt": evento["occurredAt"],
        "hash_anterior": hash_previo,
        "hash": record_hash,
        "payload": evento,
    }


def archivar_evento_inmutable(audit_record):
    if not AUDIT_ARCHIVE_BUCKET:
        return

    object_key = (
        f"events/{audit_record['occurredAt'][:10]}/"
        f"{audit_record['eventId']}.json"
    )
    s3 = boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-east-1"))
    try:
        s3.head_object(Bucket=AUDIT_ARCHIVE_BUCKET, Key=object_key)
        return
    except ClientError as error:
        status_code = error.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if status_code != 404:
            raise

    retention_until = datetime.now(timezone.utc) + timedelta(
        days=AUDIT_OBJECT_LOCK_RETENTION_DAYS
    )
    archive = {
        "event": audit_record["payload"],
        "previousHash": audit_record["hash_anterior"],
        "hash": audit_record["hash"],
    }
    s3.put_object(
        Bucket=AUDIT_ARCHIVE_BUCKET,
        Key=object_key,
        Body=json.dumps(archive, sort_keys=True, separators=(",", ":")).encode("utf-8"),
        ContentType="application/json",
        ObjectLockMode="COMPLIANCE",
        ObjectLockRetainUntilDate=retention_until,
    )


def persistir_evento_auditoria(evento):
    connection = database_connection()
    try:
        with connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT last_hash FROM audit_chain_state WHERE singleton = TRUE FOR UPDATE"
                )
                chain_state = cursor.fetchone()
                if chain_state is None:
                    raise RuntimeError("No existe el estado inicial de la cadena de auditoría.")
                hash_previo = chain_state[0]

                cursor.execute(
                    """
                    SELECT payload, previous_hash, record_hash
                    FROM audit_events WHERE event_id = %s
                    """,
                    (evento["eventId"],),
                )
                existing = cursor.fetchone()
                if existing is not None:
                    payload = existing[0]
                    if isinstance(payload, str):
                        payload = json.loads(payload)
                    return _audit_record(
                        payload, existing[1].strip(), existing[2].strip()
                    )

                record_hash = calcular_hash_evento(evento, hash_previo)
                cursor.execute(
                    """
                    INSERT INTO audit_events
                        (event_id, event_type, event_version, occurred_at,
                         correlation_id, report_id, actor_id, payload,
                         previous_hash, record_hash)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        evento["eventId"],
                        evento["eventType"],
                        evento["version"],
                        evento["occurredAt"],
                        evento["correlationId"],
                        evento["reportId"],
                        evento["data"]["actorId"],
                        Json(evento),
                        hash_previo,
                        record_hash,
                    ),
                )
                cursor.execute(
                    "UPDATE audit_chain_state SET last_hash = %s, last_event_id = %s WHERE singleton = TRUE",
                    (record_hash, evento["eventId"]),
                )
                return _audit_record(evento, hash_previo, record_hash)
    finally:
        connection.close()


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
        nuevo_bloque = persistir_evento_auditoria(evento_origen)
        archivar_evento_inmutable(nuevo_bloque)

        # Structured Logging obligatorio (Factor XI - stdout)
        LOGGER.info(json.dumps({"log_level": "INFO", "audit_record": nuevo_bloque}))

        # Confirmar procesamiento exitoso a RabbitMQ (Ack)
        ch.basic_ack(delivery_tag=method.delivery_tag)
    except Exception as e:
        LOGGER.exception("Error persistiendo auditoría: %s", e)
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
