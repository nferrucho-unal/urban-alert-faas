import pika
import json
import uuid

BROKER_LOCAL = "amqp://urban_user:urban_secure_pass@localhost:5672/%2F"

def disparar_evento_prueba_dlq():
    run_id = uuid.uuid4().hex
    params = pika.URLParameters(BROKER_LOCAL)
    connection = pika.BlockingConnection(params)
    channel = connection.channel()
    
    channel.exchange_declare(exchange='urban_alert_events', exchange_type='topic')
    
    # Enviamos múltiples mensajes para asegurar que estadísticamente algunos sean rechazados
    for i in range(1, 6):
        report_id = f"REP-2026-FALLO-{100 + i}"
        payload = {
            "reportId": report_id,
            "actor": "Andres_Felipe_Lugo",
            "descripcion": "Inundación vial severa en puente"
        }
        
        propiedades = pika.BasicProperties(
            correlation_id=f"traza-dlq-test-{run_id}-{i}",
            content_type="application/json"
        )
        
        channel.basic_publish(
            exchange='urban_alert_events',
            routing_key='reporte.creado',
            body=json.dumps(payload),
            properties=propiedades
        )
        print(f" [Core] Evento de prueba emitido para reporte: {report_id}")

    connection.close()

if __name__ == "__main__":
    disparar_evento_prueba_dlq()
