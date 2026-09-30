import json
import os
import time
import uuid

import pika
from eventos import evento_multimedia_upload
from pymongo import MongoClient

BROKER_URL = os.environ.get(
    "BROKER_URL", "amqp://urban_user:urban_secure_pass@localhost:5672/%2F"
)
MONGO_URL = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
MONGO_DATABASE = os.environ.get("MONGO_DATABASE", "urban_alert_media")
POLL_TIMEOUT_SECONDS = 30


def publicar_evento(event):
    connection = pika.BlockingConnection(pika.URLParameters(BROKER_URL))
    try:
        channel = connection.channel()
        channel.exchange_declare(exchange="urban_alert_events", exchange_type="topic", durable=True)
        channel.confirm_delivery()
        channel.basic_publish(
            exchange="urban_alert_events",
            routing_key="multimedia.upload",
            body=json.dumps(event),
            properties=pika.BasicProperties(
                correlation_id=str(uuid.uuid4()),
                content_type="application/json",
            ),
            mandatory=True,
        )
    finally:
        connection.close()


def esperar_documento(collection, object_key):
    deadline = time.monotonic() + POLL_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        document = collection.find_one({"object_key": object_key})
        if document is not None:
            return document
        time.sleep(0.25)
    raise AssertionError(f"No se encontró {object_key!r} en MongoDB")


def probar_persistencia_multimedia_nosql():
    test_id = uuid.uuid4().hex
    report_id = str(uuid.uuid4())
    object_key = f"scripts_test/{test_id}/evidencia.jpg"
    bucket = "urban-alert-evidencias-test"
    metadata = {"formato": "JPEG", "peso_bytes": 123456, "resolucion": "800x600"}
    location = {"longitude": -74.0817, "latitude": 4.6097}
    event = evento_multimedia_upload(
        report_id=report_id,
        bucket=bucket,
        object_key=object_key,
        metadata=metadata,
        location=location,
    )

    client = MongoClient(MONGO_URL, serverSelectionTimeoutMS=5000)
    try:
        client.admin.command("ping")
        collection = client[MONGO_DATABASE]["assets"]
        publicar_evento(event)
        document = esperar_documento(collection, object_key)

        assert document["report_id"] == report_id, document
        assert document["bucket"] == bucket, document
        assert document["metadata"] == metadata, document
        assert document["location"] == {
            "type": "Point",
            "coordinates": [location["longitude"], location["latitude"]],
        }, document
        assert document["mime_type"] == "image/jpeg", document
        assert document["size_bytes"] == 123456, document
        assert "original_url" not in document, document
        print(
            f"[OK] Metadata multimedia persistida en MongoDB: "
            f"report_id={report_id}, object_key={object_key}"
        )
    finally:
        client.close()


if __name__ == "__main__":
    probar_persistencia_multimedia_nosql()
