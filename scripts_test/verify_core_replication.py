import time
import psycopg2
from psycopg2.extras import RealDictCursor

# Configuración de conexiones basada en los parámetros de Gobernanza (Pág 29, 30)
DB_PRIMARY_PARAMS = {
    "host": "localhost",
    "port": 5431,
    "database": "urban_alert_core",
    "user": "app_core_user",
    "password": "app_secure_pass"
}

DB_REPLICA_PARAMS = {
    "host": "localhost",
    "port": 5433,
    "database": "urban_alert_core",
    "user": "app_core_user",
    "password": "app_secure_pass"
}

def imprimir_resultado(exito, mensaje):
    prefix = " [✓] ÉXITO:" if exito else " [×] ERROR:"
    print(f"{prefix} {mensaje}")

def test_replicacion_en_caliente():
    print("\n" + "="*70)
    print(" AUDITORÍA DE DISPONIBILIDAD: VERIFICACIÓN DE REPLICACIÓN (QAS-06)")
    print("="*70)
    
    report_id_test = f"REP-2026-REPLICA-CHECK-{int(time.time())}"
    
    # -------------------------------------------------------------------------
    # PASO 1: Escribir en la Base de Datos Primaria (Core transaccional)
    # -------------------------------------------------------------------------
    print("\nConectando a Instancia Primaria (Puerto 5431)...")
    try:
        conn_pri = psycopg2.connect(**DB_PRIMARY_PARAMS)
        cursor_pri = conn_pri.cursor()
        
        insert_query = """
            INSERT INTO reportes_core (report_id, descripcion, estado) 
            VALUES (%s, %s, %s);
        """
        cursor_pri.execute(insert_query, (report_id_test, "Falla en infraestructura crítica probando resiliencia", "CREADO"))
        conn_pri.commit()
        
        imprimir_resultado(True, f"Registro insertado en Primaria con ID: {report_id_test}")
        cursor_pri.close()
        conn_pri.close()
    except Exception as e:
        imprimir_resultado(False, f"No se pudo escribir en la primaria: {e}")
        return

    # Latencia de red local simulada corta para dar tiempo al proceso de Streaming de Postgres
    print("\n[Infraestructura Cloud] Transmitiendo registros mediante Write-Ahead Logging (WAL)...")
    time.sleep(0.8)

    # -------------------------------------------------------------------------
    # PASO 2: Leer desde la Instancia Standby/Réplica (Multi-AZ de lectura)
    # -------------------------------------------------------------------------
    print("Conectando a Instancia Réplica (Puerto 5433)...")
    try:
        conn_rep = psycopg2.connect(**DB_REPLICA_PARAMS)
        cursor_rep = conn_rep.cursor(cursor_factory=RealDictCursor)
        
        # Validar si el motor de la réplica está efectivamente en modo de solo lectura (Standby)
        cursor_rep.execute("SELECT pg_is_in_recovery();")
        is_recovery = cursor_rep.fetchone()["pg_is_in_recovery"]
        
        if is_recovery:
            print(" -> Confirmado: El contenedor Réplica opera en modo Standby (Solo Lectura/Recovery).")
        else:
            print(" -> ⚠️ ADVERTENCIA: El contenedor réplica no está en modo lectura.")

        # Consultar el reporte insertado desde la primaria
        select_query = "SELECT * FROM reportes_core WHERE report_id = %s;"
        cursor_rep.execute(select_query, (report_id_test,))
        registro_replicado = cursor_rep.fetchone()
        
        if registro_replicado:
            imprimir_resultado(True, "¡Sincronización Confirmada en Caliente!")
            print(f" -> Registro recuperado con éxito: [{registro_replicado['report_id']}] Estado: {registro_replicado['estado']}")
            print(f" -> RPO evaluado: Menor a 1 segundo (Objetivo de Gobernanza: < 15 minutos).")
        else:
            imprimir_resultado(False, f"El registro {report_id_test} no se encontró en la réplica (Pérdida de consistencia o retraso en replicación).")
            
        cursor_rep.close()
        conn_rep.close()
        
    except Exception as e:
        imprimir_resultado(False, f"No se pudo consultar la instancia réplica: {e}")

    # -------------------------------------------------------------------------
    # PASO 3: Demostración de Privilegios de Solo Lectura en la Réplica (Gobernanza)
    # -------------------------------------------------------------------------
    print("\nVerificando restricción de gobernanza (Intentar escribir en la Réplica)...")
    try:
        conn_rep = psycopg2.connect(**DB_REPLICA_PARAMS)
        cursor_rep = conn_rep.cursor()
        cursor_rep.execute("INSERT INTO usuarios (username, rol) VALUES ('fail_user', 'admin');")
        conn_rep.commit()
        imprimir_resultado(False, "Se permitió escribir en la réplica. Configuración errónea de infraestructura.")
    except psycopg2.errors.ReadOnlySqlTransaction:
        imprimir_resultado(True, "Transacción rechazada de forma segura por el motor Standby (ReadOnlySqlTransaction).")
    except Exception as e:
        print(f" -> Capturado error esperado de base de datos.")
    finally:
        print("="*70 + "\n")

if __name__ == "__main__":
    test_replicacion_en_caliente()
