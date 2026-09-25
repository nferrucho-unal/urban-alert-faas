import os
import json
import random
import pika

BROKER_URL = os.environ.get("BROKER_URL", "amqp://urban_user:urban_secure_pass@localhost:5672/%2F")

def callback_notificaciones_con_dlq(ch, method, properties, body):
    """Manejador reactivo asíncrono con desvío automático a DLQ por infraestructura (QAS-02)."""
    evento = json.loads(body.decode('utf-8'))
    report_id = evento.get("reportId")
    
    print(f" [FaaS Notificaciones] Procesando alerta para reporte: {report_id}", flush=True)
    
    # Simulación estricta de la caída del proveedor externo (SendGrid/FCM)
    # Forzamos una simulación de fallo definitivo para verificar que caiga en la DLQ
    if random.random() < 0.4:  
        print(f" [❌ FALLO CRÍTICO] Proveedor externo inactivo. Rechazando mensaje permanentemente...", flush=True)
        
        # TÁCTICA: basic_reject con requeue=False le indica a RabbitMQ que el mensaje murió.
        # Al estar configurada la cola con x-dead-letter-exchange, el bus lo desvía automáticamente a la DLQ.
        ch.basic_reject(delivery_tag=method.delivery_tag, requeue=False)
    else:
        print(f" [✓ ÉXITO] Alerta despachada correctamente para el reporte {report_id}", flush=True)
        ch.basic_ack(delivery_tag=method.delivery_tag)

def iniciar_consumidor():
    params = pika.URLParameters(BROKER_URL)
    connection = pika.BlockingConnection(params)
    channel = connection.channel()
    
    # 1. DEFINICIÓN DEL CANAL DE MENSAJES MUERTOS (DLQ)
    # Declaramos el Exchange y la Cola dedicados exclusivamente a capturar fallos (Página 27)
    channel.exchange_declare(exchange='urban_alert_dlx', exchange_type='topic')
    channel.queue_declare(queue='q_dead_letter_notifications', durable=True)
    channel.queue_bind(
        exchange='urban_alert_dlx', 
        queue='q_dead_letter_notifications', 
        routing_key='notificaciones.failed'
    )
    
    # 2. DEFINICIÓN DEL CANAL PRINCIPAL DE PRODUCCIÓN
    channel.exchange_declare(exchange='urban_alert_events', exchange_type='topic')
    
    # CONFIGURACIÓN RÍGIDA DE LA COLA PRINCIPAL PARA INTEGRAR LA DLQ AUTOMÁTICA
    argumentos_infraestructura = {
        'x-dead-letter-exchange': 'urban_alert_dlx',
        'x-dead-letter-routing-key': 'notificaciones.failed'
    }
    
    channel.queue_declare(
        queue='q_notificaciones_core', 
        durable=True, 
        arguments=argumentos_infraestructura
    )
    
    channel.queue_bind(
        exchange='urban_alert_events', 
        queue='q_notificaciones_core', 
        routing_key='reporte.creado'
    )
    
    print(" [*] FaaS Notificaciones escuchando con DLQ activa por hardware AMQP.", flush=True)
    channel.basic_consume(queue='q_notificaciones_core', on_message_callback=callback_notificaciones_con_dlq)
    channel.start_consuming()

if __name__ == "__main__":
    iniciar_consumidor()
