import json
import os
import sys
from datetime import datetime, timezone

import pika
from pymongo import ASCENDING, MongoClient
from pymongo.errors import PyMongoError
from validar.event_contracts import (
    ContractValidationError,
    UnsupportedEventError,
    dead_letter_invalid_message,
    validate_event,
)

BROKER_URL = os.environ.get(
    "BROKER_URL", "amqp://urban_user:urban_secure_pass@localhost:5672/%2F"
)
MONGO_URL = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
MONGO_DATABASE = os.environ.get("MONGO_DATABASE", "urban_alert_media")
MONGO_COLLECTION = os.environ.get("MONGO_COLLECTION", "assets")
mongo_client = MongoClient(MONGO_URL, serverSelectionTimeoutMS=5000)


def inicializar_almacen_metadata():
    mongo_client.admin.command("ping")
    collection = mongo_client[MONGO_DATABASE][MONGO_COLLECTION]
    collection.create_index([("object_key", ASCENDING)], unique=True)
    collection.create_index([("location", "2dsphere")])


def persistir_metadatos(documento):
    collection = mongo_client[MONGO_DATABASE][MONGO_COLLECTION]
    collection.update_one(
        {"object_key": documento["object_key"]},
        {
            "$set": documento,
            "$setOnInsert": {"created_at": documento["updated_at"]},
        },
        upsert=True,
    )


def callback_multimedia(ch, method, properties, body):
    """Valida multimedia.upload y guarda referencias/metadata fuera de PostGIS."""
    try:
        evento = validate_event(
            json.loads(body.decode("utf-8")), "multimedia.upload"
        )
    except UnsupportedEventError as error:
        print(f"Evento multimedia ignorado: {error}", file=sys.stderr, flush=True)
        ch.basic_ack(delivery_tag=method.delivery_tag)
        return
    except (UnicodeDecodeError, json.JSONDecodeError, ContractValidationError) as error:
        print(f"Evento multimedia inválido: {error}", file=sys.stderr, flush=True)
        dead_letter_invalid_message(
            ch, method, properties, body, "multimedia.invalid", str(error)
        )
        return

    data = evento["data"]
    report_id = evento["reportId"]
    object_key = data["objectKey"]
    bucket = data["bucket"]

    try:
        object_path = object_key.lstrip("/")
        metadata = data.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}

        document = {
            "report_id": report_id,
            "object_key": object_key,
            "bucket": bucket,
            "metadata": metadata,
            "mime_type": data["mimeType"],
            "size_bytes": data.get("tamanoBytes"),
            "updated_at": datetime.now(timezone.utc),
        }
        location = data.get("location")
        if isinstance(location, dict):
            longitude = location.get("longitude")
            latitude = location.get("latitude")
            if isinstance(longitude, (int, float)) and isinstance(latitude, (int, float)):
                if -180 <= longitude <= 180 and -90 <= latitude <= 90:
                    document["location"] = {
                        "type": "Point",
                        "coordinates": [longitude, latitude],
                    }

        persistir_metadatos(document)
        print(
            json.dumps(
                {
                    "log_level": "INFO",
                    "component": "FaaS_Multimedia",
                    "message": "Referencia y metadata multimedia persistidas en NoSQL.",
                    "reportId": report_id,
                    "objectKey": object_key,
                }
            ),
            flush=True,
        )
        ch.basic_ack(delivery_tag=method.delivery_tag)
    except PyMongoError as error:
        print(f"Error persistiendo metadata en MongoDB: {error}", file=sys.stderr, flush=True)
        ch.basic_nack(delivery_tag=method.delivery_tag, requeue=True)
    except Exception as error:
        print(f"Error procesando multimedia: {error}", file=sys.stderr, flush=True)
        ch.basic_nack(delivery_tag=method.delivery_tag, requeue=True)


def iniciar_consumidor():
    inicializar_almacen_metadata()
    connection = pika.BlockingConnection(pika.URLParameters(BROKER_URL))
    channel = connection.channel()
    channel.basic_qos(prefetch_count=1)
    channel.exchange_declare(exchange="urban_alert_events", exchange_type="topic", durable=True)
    channel.exchange_declare(exchange="urban_alert_dlx", exchange_type="topic", durable=True)
    channel.queue_declare(queue="q_dead_letter_multimedia", durable=True)
    channel.queue_bind(
        exchange="urban_alert_dlx",
        queue="q_dead_letter_multimedia",
        routing_key="multimedia.invalid",
    )
    channel.queue_declare(queue="q_multimedia_processing", durable=True)
    channel.queue_bind(
        exchange="urban_alert_events",
        queue="q_multimedia_processing",
        routing_key="multimedia.upload",
    )

    print(
        " [*] FaaS Multimedia escuchando en 'q_multimedia_processing'.",
        flush=True,
    )
    channel.basic_consume(
        queue="q_multimedia_processing", on_message_callback=callback_multimedia
    )
    channel.confirm_delivery()
    channel.start_consuming()


if __name__ == "__main__":
    iniciar_consumidor()
