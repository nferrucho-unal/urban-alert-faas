import pika
import json
import time
import uuid

BROKER_LOCAL = "amqp://urban_user:urban_secure_pass@localhost:5672/%2F"

def ejecutar_ataque_tampering():
    params = pika.URLParameters(BROKER_LOCAL)
    connection = pika.BlockingConnection(params)
    channel = connection.channel()
    
    channel.exchange_declare(exchange='urban_alert_events', exchange_type='topic')
    
    report_id = "REP-2026-ALERTA-88"
    correlation_id_fijo = f"tampering-{uuid.uuid4()}"
    
    # 1. ENVÍO ORIGINAL: Datos auténticos del ciudadano
    payload_autentico = {
        "reportId": report_id,
        "actor": "Mario_Bross",
        "descripcion": "Daño severo en alcantarillado central"
    }
    
    propiedades = pika.BasicProperties(correlation_id=correlation_id_fijo, content_type="application/json")
    
    print(" [Core] Enviando evento legítimo original al Bus...")
    channel.basic_publish(exchange='urban_alert_events', routing_key='reporte.creado', 
                          body=json.dumps(payload_autentico), properties=propiedades)
    
    time.sleep(1.5)
    
    # 2. ENVÍO ALTERADO (Ataque/Inyección): Mismo ID de mensaje, pero alterando la descripción o datos del reporte
    payload_adulterado = {
        "reportId": report_id,
        "actor": "Mario_Bross",
        "descripcion": "INYECCIÓN DE PAYLOAD ALTERADO - CAMBIO DE ATRIBUTOS" # Modificación del cuerpo
    }
    
    print(" [⚠️ Hacker] Intentando enviar payload alterado reutilizando el ID de correlación...")
    channel.basic_publish(exchange='urban_alert_events', routing_key='reporte.creado', 
                          body=json.dumps(payload_adulterado), properties=propiedades)
    
    connection.close()

if __name__ == "__main__":
    ejecutar_ataque_tampering()