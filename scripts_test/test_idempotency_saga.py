import pika
import json
import time

BROKER_LOCAL = "amqp://urban_user:urban_secure_pass@localhost:5672/%2F"

def probar_ataque_duplicados():
    params = pika.URLParameters(BROKER_LOCAL)
    connection = pika.BlockingConnection(params)
    channel = connection.channel()
    
    channel.exchange_declare(exchange='urban_alert_events', exchange_type='topic')
    
    report_id = "REP-2026-BOGOTA-1001"
    # ID único de correlación compartido para simular el duplicado exacto del bus de eventos
    correlation_id_compartido = "id-unico-de-mensaje-transaccional-001"
    
    payload = {
        "reportId": report_id,
        "actor": "Diana_Reyes_Marciales",
        "descripcion": "Daño severo en alumbrado público"
    }
    
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
