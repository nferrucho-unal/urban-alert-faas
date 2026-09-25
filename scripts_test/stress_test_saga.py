import json
import os
import sys
import threading
import time
import pika

# Parámetros de conexión AMQP
BROKER_LOCAL = "amqp://urban_user:urban_secure_pass@localhost:5672//"
EXCHANGE_NAME = "urban_alert_events"

# Configuración del Stress Test (QAS-03: Picos 10x de carga en una ráfaga masiva)
EVENTOS_POR_SEGUNDO = 500
DURACION_SEGUNDOS = 3
TOTAL_EVENTOS = EVENTOS_POR_SEGUNDO * DURACION_SEGUNDOS


def enviar_evento_worker(worker_id, start_idx, num_eventos, correlation_id_base):
    """Worker que abre un canal dedicado para inyectar un lote de eventos a máxima velocidad."""
    try:
        params = pika.URLParameters(BROKER_LOCAL)
        connection = pika.BlockingConnection(params)
        channel = connection.channel()

        # Asegurar la existencia del exchange en el broker
        channel.exchange_declare(exchange=EXCHANGE_NAME, exchange_type="topic")

        for i in range(num_eventos):
            current_idx = start_idx + i
            report_id = f"REP-2026-STRESS-{current_idx}"
            correlation_id = f"{correlation_id_base}-{current_idx}"

            # Construcción de los payloads transaccionales y multimedia
            payload_reporte = {
                "reportId": report_id,
                "actor": f"Stress_Worker_{worker_id}",
                "descripcion": "Simulación masiva de colapso de infraestructura vial.",
            }

            propiedades = pika.BasicProperties(
                correlation_id=correlation_id,
                content_type="application/json",
                delivery_mode=1,  # Mensaje efímero/Transient en memoria para acelerar el stress test
            )

            # 1. Inyectar evento para Auditoría y Notificaciones
            channel.basic_publish(
                exchange=EXCHANGE_NAME,
                routing_key="reporte.creado",
                body=json.dumps(payload_reporte),
                properties=propiedades,
            )

            # 2. Inyectar evento paralelo para el Servicio Multimedia
            payload_multimedia = {
                "reportId": report_id,
                "bucket": "urban-alert-evidencias-bogota",
                "objectKey": f"reportes/fotos/evidencia_stress_{current_idx}.jpg",
            }
            channel.basic_publish(
                exchange=EXCHANGE_NAME,
                routing_key="multimedia.upload",
                body=json.dumps(payload_multimedia),
                properties=propiedades,
            )

        connection.close()
    except Exception as e:
        print(f"\n[×] Error en el Worker {worker_id}: {e}", file=sys.stderr)


def ejecutar_stress_test():
    print("\n" + "=" * 75)
    print(
        f" DISPARANDO STRESS TEST: {EVENTOS_POR_SEGUNDO} EVENTOS/SEG | TOTAL: {TOTAL_EVENTOS} RELEASES"
    )
    print(" OBJETIVO DE CALIDAD: Evaluar Autoescala y Latencia bajo picos 10x (QAS-03)")
    print("=" * 75 + "\n")

    num_workers = 10  # Dividir la carga en 10 hilos concurrentes
    eventos_per_worker_seg = EVENTOS_POR_SEGUNDO // num_workers
    correlation_base = "trace-stress-2026"

    start_time_global = time.time()

    for segundo in range(DURACION_SEGUNDOS):
        print(
            f"🚀 [Segundo {segundo+1}/{DURACION_SEGUNDOS}] Inyectando ráfaga de {EVENTOS_POR_SEGUNDO} mensajes al bus..."
        )
        threads = []
        base_index_segundo = segundo * EVENTOS_POR_SEGUNDO

        start_time_rafaga = time.time()

        for w_id in range(num_workers):
            start_idx = (
                base_index_segundo + (w_id * eventos_per_worker_seg) + 1
            )
            t = threading.Thread(
                target=enviar_evento_worker,
                args=(
                    w_id,
                    start_idx,
                    eventos_per_worker_seg,
                    f"{correlation_base}-seg{segundo}",
                ),
            )
            threads.append(t)
            t.start()

        # Esperar a que todos los hilos terminen la ráfaga de este segundo
        for t in threads:
            t.join()

        # Calcular el remanente de tiempo para mantener la cadencia exacta de 1 segundo
        elapsed_rafaga = time.time() - start_time_rafaga
        sleep_time = 1.0 - elapsed_rafaga
        if sleep_time > 0:
            time.sleep(sleep_time)

    duration_total = time.time() - start_time_global
    print("\n" + "-" * 75)
    print(f" [✓] Inyección masiva finalizada con éxito.")
    print(
        f" -> Mensajes enviados: {TOTAL_EVENTOS * 2} (Transaccionales + Multimedia)"
    )
    print(f" -> Tiempo total de ejecución del emisor: {duration_total:.2f} s")
    print(
        " -> Revise la consola web de RabbitMQ y las terminales de Docker para analizar la contención."
    )
    print("-" * 75 + "\n")


if __name__ == "__main__":
    ejecutar_stress_test()
