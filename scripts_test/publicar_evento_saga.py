import json
import pika

BROKER_LOCAL = "amqp://urban_user:urban_secure_pass@localhost:5672/%2F"


def simular_flujo_completo_core():
    params = pika.URLParameters(BROKER_LOCAL)
    connection = pika.BlockingConnection(params)
    channel = connection.channel()

    channel.exchange_declare(exchange="urban_alert_events", exchange_type="topic")

    report_id = "REP-2026-BOGOTA-777"
    correlation_id = "saga-flow-multimedia-test-uuid"

    # PROPAGACIÓN DE IDENTIFICADORES (Distributed Tracing - QAS-04.1)
    propiedades = pika.BasicProperties(
        correlation_id=correlation_id, content_type="application/json"
    )

    # --- EVENTO 1: Creación del Reporte Transaccional ---
    payload_reporte = {
        "reportId": report_id,
        "actor": "Julian_Rivas_Sanchez",
        "descripcion": "Falla semafórica severa con riesgo de colisión",
    }
    channel.basic_publish(
        exchange="urban_alert_events",
        routing_key="reporte.creado",
        body=json.dumps(payload_reporte),
        properties=propiedades,
    )
    print(" [Core] Evento 'reporte.creado' enviado al bus.")

    # --- EVENTO 2: Simulación de Carga Exitosa en S3 (Decouple Persistence - QAS-03) ---
    payload_multimedia = {
        "reportId": report_id,
        "bucket": "urban-alert-evidencias-bogota",
        "objectKey": "reportes/fotos/semaforo_roto_calle_100.jpg",
    }
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
