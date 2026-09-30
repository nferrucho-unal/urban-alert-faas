import pika
import json
import time
import uuid
import copy
from eventos import evento_reporte_creado

BROKER_LOCAL = "amqp://urban_user:urban_secure_pass@localhost:5672/%2F"

def ejecutar_ataque_tampering():
    params = pika.URLParameters(BROKER_LOCAL)
    connection = pika.BlockingConnection(params)
    channel = connection.channel()
    
    channel.exchange_declare(exchange='urban_alert_events', exchange_type='topic', durable=True)
    
    report_id = str(uuid.uuid4())
    event_id = str(uuid.uuid4())
    correlation_id_fijo = str(uuid.uuid4())
    
    # 1. ENVÍO ORIGINAL: Datos auténticos del ciudadano
    payload_autentico = evento_reporte_creado(
        report_id=report_id,
        event_id=event_id,
        correlation_id=correlation_id_fijo,
    )
    
    propiedades = pika.BasicProperties(correlation_id=correlation_id_fijo, content_type="application/json")
    
    print(" [Core] Enviando evento legítimo original al Bus...")
    channel.basic_publish(exchange='urban_alert_events', routing_key='reporte.creado', 
                          body=json.dumps(payload_autentico), properties=propiedades)
    
    time.sleep(1.5)
    
    # 2. Reutilizar el eventId con un cuerpo distinto debe disparar la DLQ.
    payload_adulterado = copy.deepcopy(payload_autentico)
    payload_adulterado["data"]["lat"] = 4.7
    
    print(" [⚠️ Hacker] Intentando enviar payload alterado reutilizando el ID de correlación...")
    channel.basic_publish(exchange='urban_alert_events', routing_key='reporte.creado', 
                          body=json.dumps(payload_adulterado), properties=propiedades)
    
    connection.close()

if __name__ == "__main__":
    ejecutar_ataque_tampering()