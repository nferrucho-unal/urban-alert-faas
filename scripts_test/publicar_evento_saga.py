import json
import uuid
import pika
from eventos import evento_multimedia_upload, evento_reporte_creado

BROKER_LOCAL = "amqp://urban_user:urban_secure_pass@localhost:5672/%2F"


def simular_flujo_completo_core():
    params = pika.URLParameters(BROKER_LOCAL)
    connection = pika.BlockingConnection(params)
    channel = connection.channel()

    channel.exchange_declare(exchange="urban_alert_events", exchange_type="topic", durable=True)

    report_id = str(uuid.uuid4())
    correlation_id = str(uuid.uuid4())

    # PROPAGACIÓN DE IDENTIFICADORES (Distributed Tracing - QAS-04.1)
    propiedades = pika.BasicProperties(
        correlation_id=correlation_id, content_type="application/json"
    )

    # --- EVENTO 1: Creación del Reporte Transaccional ---
    payload_reporte = evento_reporte_creado(
        report_id=report_id, correlation_id=correlation_id
    )
    channel.basic_publish(
        exchange="urban_alert_events",
        routing_key="reporte.creado",
        body=json.dumps(payload_reporte),
        properties=propiedades,
    )
    print(" [Core] Evento 'reporte.creado' enviado al bus.")

    # --- EVENTO 2: Simulación de Carga Exitosa en S3 (Decouple Persistence - QAS-03) ---
    payload_multimedia = evento_multimedia_upload(
        report_id=report_id,
        correlation_id=correlation_id,
        bucket="urban-alert-evidencias-bogota",
        object_key=f"reportes/{report_id}/semaforo_roto_calle_100.jpg",
        metadata={"formato": "JPEG"},
        location={"longitude": -74.05, "latitude": 4.65},
    )
    channel.basic_publish(
        exchange="urban_alert_events",
        routing_key="multimedia.upload",
        body=json.dumps(payload_multimedia),
        properties=propiedades,
    )
    print(" [Storage S3] Evento 'multimedia.upload' enviado al bus.")

    connection.close()


if __name__ == "__main__":
    simular_flujo_completo_core()
