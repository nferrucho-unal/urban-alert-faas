import json
import os
import pika

BROKER_URL = os.environ.get(
    "BROKER_URL", "amqp://urban_user:urban_secure_pass@localhost:5672/%2F"
)


def callback_multimedia(ch, method, properties, body):
    """Manejador FaaS disparado por un evento de carga de imágenes (QAS-03)."""
    evento_s3 = json.loads(body.decode("utf-8"))
    report_id = evento_s3.get("reportId")
    object_key = evento_s3.get("objectKey")

    print(
        f" [FaaS Multimedia] Nueva imagen detectada en Storage para Reporte: {report_id}",
        flush=True,
    )
    print(f" [FaaS Multimedia] Procesando archivo: {object_key}", flush=True)

    # Simulación de la lógica de negocio (Tácticas QAS-03 / Polyglot Persistence):
    # 1. Extracción de metadatos EXIF y GPS
    # 2. Generación de miniaturas (Thumbnails) para optimizar carga
    metadatos_exif_nosql = {
        "reportId": report_id,
        "s3_original_url": f"https://amazonaws.com{object_key}",
        "s3_thumbnail_url": f"https://amazonaws.comthumbnails/thumb_{os.path.basename(object_key)}",
        "metadata": {
            "resolucion": "4032x3024",
            "peso_bytes": 1845200,
            "formato": "JPEG",
            "gps_extrapolado": {"latitud": 4.6097, "longitud": -74.0817},
        },
    }

    # Impresión obligatoria de Log Estructurado (Factor XI - QAS-04.1)
    log_payload = {
        "log_level": "INFO",
        "component": "FaaS_Multimedia",
        "message": "Metadatos extraídos y miniatura guardada en Object Storage.",
        "record_nosql_flex": metadatos_exif_nosql,
    }
    print(json.dumps(log_payload), flush=True)

    # Confirmar a RabbitMQ que el procesamiento de la imagen finalizó con éxito
    ch.basic_ack(delivery_tag=method.delivery_tag)


def iniciar_consumidor():
    params = pika.URLParameters(BROKER_URL)
    connection = pika.BlockingConnection(params)
    channel = connection.channel()

    # Declarar e interconectar el Exchange de eventos de Urban Alert
    channel.exchange_declare(exchange="urban_alert_events", exchange_type="topic")
    channel.queue_declare(queue="q_multimedia_processing", durable=True)

    # Nos suscribimos específicamente a eventos de subida multimedia
    channel.queue_bind(
        exchange="urban_alert_events",
        queue="q_multimedia_processing",
        routing_key="multimedia.upload",
    )

    print(
        " [*] FaaS Multimedia esperando eventos en 'q_multimedia_processing'.",
        flush=True,
    )
    channel.basic_consume(
        queue="q_multimedia_processing", on_message_callback=callback_multimedia
    )
    channel.start_consuming()


if __name__ == "__main__":
    iniciar_consumidor()
