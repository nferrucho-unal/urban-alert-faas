import psycopg2
from psycopg2.extras import RealDictCursor

# Conexión local usando el usuario de gobernanza restringido (Solo Lectura)
DB_PARAMS = {
    "host": "localhost",
    "port": 5432,
    "database": "urban_alert_geo",
    "user": "analista_obras",
    "password": "read_only_gis_pass"
}

def consultar_mapa_por_area_visible():
    """Simula una ráfaga de consulta del mapa filtrando por coordenadas visibles (QAS-05)."""
    conn = psycopg2.connect(**DB_PARAMS)
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    
    # Coordenadas de la caja delimitadora (Bounding Box) que el analista está viendo en su pantalla
    # Definimos un polígono que encierra el sector norte/centro de Bogotá
    lon_min, lat_min = -74.0900, 4.6000
    lon_max, lat_max = -74.0600, 4.6500
    
    # Consulta SQL Parametrizada eliminando riesgo de inyección (QAS-01.2)
    # Usa ST_MakeEnvelope para construir la caja y el operador espacial '&&' indexado
    sql_query = """
        SELECT report_id, tipo_daño, estado, 
               ST_X(ubicacion) as longitud, ST_Y(ubicacion) as latitud
        FROM reportes_geograficos
        WHERE ubicacion && ST_MakeEnvelope(%s, %s, %s, %s, 4326);
    """
    
    try:
        import time
        start_time = time.time()
        
        cursor.execute(sql_query, (lon_min, lat_min, lon_max, lat_max))
        resultados = cursor.fetchall()
        
        latency_ms = (time.time() - start_time) * 1000
        
        print(f"\n[✓ Servidor Geoespacial Aislado] Consulta ejecutada con éxito.")
        print(f"Latencia percibida: {latency_ms:.2f} ms (Objetivo de Gobernanza: < 200ms)")
        print(f"Reportes encontrados en el cuadrante visualizado ({len(resultados)}):")
        print("-" * 75)
        for row in resultados:
            print(f"ID: {row['report_id']} | Tipo: {row['tipo_daño']} | Estado: {row['estado']} | Coord: ({row['latitud']}, {row['longitud']})")
            
    except Exception as e:
        print(f"Error en la consulta espacial: {e}")
    finally:
        cursor.close()
        conn.close()

if __name__ == "__main__":
    consultar_mapa_por_area_visible()
