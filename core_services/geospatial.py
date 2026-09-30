import os

from flask import Flask, g, jsonify, request
from psycopg2.extras import RealDictCursor

from core_services.common import (
    ApiError,
    database_connection,
    install_request_context,
    register_error_handlers,
    require_roles,
)

app = Flask(__name__)
install_request_context(app)
register_error_handlers(app)


@app.get("/health")
def health():
    return jsonify({"status": "ok", "service": "urban-alert-geospatial"})


@app.get("/geospatial/reportes")
@require_roles("gestor", "admin")
def reports_in_bbox():
    bbox = request.args.get("bbox", "")
    parts = bbox.split(",")
    if len(parts) != 4:
        raise ApiError(400, "VALIDATION_ERROR", "bbox debe ser minLon,minLat,maxLon,maxLat.")
    try:
        min_lon, min_lat, max_lon, max_lat = map(float, parts)
    except ValueError:
        raise ApiError(400, "VALIDATION_ERROR", "bbox contiene coordenadas inválidas.")
    if not (-180 <= min_lon < max_lon <= 180 and -90 <= min_lat < max_lat <= 90):
        raise ApiError(400, "VALIDATION_ERROR", "bbox está fuera de rango o invertido.")

    limit = request.args.get("limit", default=500, type=int)
    if limit < 1 or limit > 1000:
        raise ApiError(400, "VALIDATION_ERROR", "limit debe estar entre 1 y 1000.")

    connection = database_connection(geo=True)
    try:
        with connection.cursor(cursor_factory=RealDictCursor) as cursor:
            filters = ["geom && ST_MakeEnvelope(%s, %s, %s, %s, 4326)"]
            parameters = [min_lon, min_lat, max_lon, max_lat]
            if g.user_context["role"] == "gestor":
                filters.append("municipio_id = %s")
                parameters.append(g.user_context["municipio_id"])
            cursor.execute(
                f"""
                SELECT reporte_id, municipio_id, categoria, estado, creado_en,
                       ST_X(geom) AS lon, ST_Y(geom) AS lat
                FROM reportes_geo
                WHERE {' AND '.join(filters)}
                ORDER BY creado_en DESC, reporte_id
                LIMIT %s
                """,
                (*parameters, limit),
            )
            rows = cursor.fetchall()
        features = [
            {
                "type": "Feature",
                "geometry": {
                    "type": "Point",
                    "coordinates": [row["lon"], row["lat"]],
                },
                "properties": {
                    "reportId": str(row["reporte_id"]),
                    "municipioId": str(row["municipio_id"]),
                    "categoria": row["categoria"],
                    "estado": row["estado"],
                    "creadoEn": row["creado_en"].isoformat(),
                },
            }
            for row in rows
        ]
        return jsonify({"type": "FeatureCollection", "features": features})
    finally:
        connection.close()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")))