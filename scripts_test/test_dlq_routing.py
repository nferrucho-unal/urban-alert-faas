import pika
import json
from eventos import evento_reporte_creado

BROKER_LOCAL = "amqp://urban_user:urban_secure_pass@localhost:5672/%2F"

def disparar_evento_prueba_dlq():
    params = pika.URLParameters(BROKER_LOCAL)
    connection = pika.BlockingConnection(params)
    channel = connection.channel()
    
    channel.exchange_declare(exchange='urban_alert_events', exchange_type='topic', durable=True)
    
    # Enviamos múltiples mensajes para asegurar que estadísticamente algunos sean rechazados
    for i in range(1, 6):
        payload = evento_reporte_creado()
        report_id = payload["reportId"]
        
        propiedades = pika.BasicProperties(
            correlation_id=payload["correlationId"],
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
