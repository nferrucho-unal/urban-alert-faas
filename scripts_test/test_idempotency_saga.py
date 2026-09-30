import pika
import json
import time
import uuid
from eventos import evento_reporte_creado

BROKER_LOCAL = "amqp://urban_user:urban_secure_pass@localhost:5672/%2F"

def probar_ataque_duplicados():
    params = pika.URLParameters(BROKER_LOCAL)
    connection = pika.BlockingConnection(params)
    channel = connection.channel()
    
    channel.exchange_declare(exchange='urban_alert_events', exchange_type='topic', durable=True)
    
    report_id = str(uuid.uuid4())
    correlation_id_compartido = str(uuid.uuid4())
    payload = evento_reporte_creado(
        report_id=report_id, correlation_id=correlation_id_compartido
    )
    
    propiedades = pika.BasicProperties(
        correlation_id=correlation_id_compartido,
        content_type="application/json"
    )
    
    # ENVÍO 1: Procesamiento Normal
    print(" [Core] Enviando Petición Original del Evento...")
    channel.basic_publish(
        exchange='urban_alert_events',
        routing_key='reporte.creado',
        body=json.dumps(payload),
        properties=propiedades
    )
    
    # Esperamos un instante corto para asegurar el orden secuencial en el entorno local
    time.sleep(1)
    
    # ENVÍO 2: Simulación de mensaje duplicado (Retry storm del bus)
    print(" [Core] Enviando Petición Duplicada (Falla de red simulada en confirmación del bus)...")
    channel.basic_publish(
        exchange='urban_alert_events',
        routing_key='reporte.creado',
        body=json.dumps(payload),
        properties=propiedades
    )
    
    connection.close()

if __name__ == "__main__":
    probar_ataque_duplicados()
