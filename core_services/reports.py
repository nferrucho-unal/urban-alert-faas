import json
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import requests
from botocore.exceptions import BotoCoreError, ClientError
from flask import Blueprint, g, request
from psycopg2.extras import RealDictCursor

from core_services.common import (
    ApiError,
    create_event,
    database_connection,
    enqueue_event,
    parse_uuid,
    require_json_object,
    require_roles,
)
from core_services.storage import (
    ALLOWED_MIME_TYPES,
    DOWNLOAD_URL_TTL_SECONDS,
    MAX_UPLOAD_BYTES,
    UPLOAD_URL_TTL_SECONDS,
    s3_bucket,
    s3_client,
)

reports = Blueprint("reports", __name__)
LOGGER = logging.getLogger(__name__)
VALID_CATEGORIES = {"hueco_via", "fuga_agua", "alumbrado", "arbolado", "otro"}
VALID_TRANSITIONS = {
    "RECIBIDO": {"VALIDADO", "RECHAZADO"},
    "VALIDADO": {"RECHAZADO"},
    "EN_OBRA": {"RESUELTO"},
}
TRANSITION_EVENT_TYPES = {
    "VALIDADO": "reporte.validado",
    "RECHAZADO": "reporte.rechazado",
    "RESUELTO": "reporte.resuelto",
}


def _report_dict(row):
    result = dict(row)
    for key, value in result.items():
        if hasattr(value, "isoformat"):
            result[key] = value.isoformat()
        elif isinstance(value, Decimal):
            result[key] = float(value)
        elif isinstance(value, uuid.UUID):
            result[key] = str(value)
    return result


def _return_idempotent_report(existing, actor_id, category, description, municipality_id, latitude, longitude):
    if str(existing["actor_id"]) != actor_id:
        raise ApiError(409, "IDEMPOTENCY_KEY_REUSED", "correlationId ya fue utilizado.")
    same_request = (
        existing["categoria"] == category
        and existing["descripcion"] == description
        and str(existing["municipio_id"]) == municipality_id
        and existing["lat"] == latitude
        and existing["lon"] == longitude
    )
    if not same_request:
        raise ApiError(409, "IDEMPOTENCY_KEY_REUSED", "correlationId ya fue utilizado con otros datos.")
    return {"report": _report_dict(existing)}, 200


@reports.post("/reportes")
@require_roles("ciudadano")
def create_report():
    body = require_json_object()
    description = body.get("descripcion")
    category = body.get("categoria")
    location = body.get("ubicacion")
    if not isinstance(description, str) or not 10 <= len(description.strip()) <= 500:
        raise ApiError(400, "VALIDATION_ERROR", "descripcion debe tener entre 10 y 500 caracteres.")
    if not isinstance(category, str) or category not in VALID_CATEGORIES:
        raise ApiError(400, "VALIDATION_ERROR", "categoria no está permitida.")
    if not isinstance(location, dict):
        raise ApiError(400, "VALIDATION_ERROR", "ubicacion es obligatoria.")
    try:
        if isinstance(location.get("lat"), bool) or isinstance(location.get("lon"), bool):
            raise ValueError("boolean coordinates are invalid")
        latitude = float(location["lat"])
        longitude = float(location["lon"])
    except (KeyError, TypeError, ValueError):
        raise ApiError(400, "VALIDATION_ERROR", "ubicacion requiere lat y lon numéricos.")
    if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
        raise ApiError(400, "VALIDATION_ERROR", "Coordenadas fuera de rango.")

    municipality_id = parse_uuid(body.get("municipio_id"), "municipio_id")
    context = g.user_context
    if context.get("municipio_id") and context["municipio_id"] != municipality_id:
        raise ApiError(403, "FORBIDDEN", "El reporte debe pertenecer a su municipio.")
    correlation_id = parse_uuid(body.get("correlationId"), "correlationId")
    report_id = str(uuid.uuid4())
    connection = database_connection()
    try:
        with connection:
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute("SELECT id FROM municipios WHERE id = %s AND activo", (municipality_id,))
                if cursor.fetchone() is None:
                    raise ApiError(422, "MUNICIPIO_NOT_FOUND", "El municipio no existe o está inactivo.")

                cursor.execute(
                    "SELECT * FROM reportes_core WHERE correlation_id = %s",
                    (correlation_id,),
                )
                existing = cursor.fetchone()
                if existing:
                    return _return_idempotent_report(
                        existing,
                        context["sub"],
                        category,
                        description.strip(),
                        municipality_id,
                        latitude,
                        longitude,
                    )

                cursor.execute(
                    """
                    INSERT INTO reportes_core
                        (report_id, descripcion, estado, categoria, actor_id,
                         municipio_id, lat, lon, correlation_id)
                    VALUES (%s, %s, 'RECIBIDO', %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (correlation_id) DO NOTHING
                    RETURNING *
                    """,
                    (
                        report_id,
                        description.strip(),
                        category,
                        context["sub"],
                        municipality_id,
                        latitude,
                        longitude,
                        correlation_id,
                    ),
                )
                created = cursor.fetchone()
                if created is None:
                    cursor.execute(
                        "SELECT * FROM reportes_core WHERE correlation_id = %s",
                        (correlation_id,),
                    )
                    existing = cursor.fetchone()
                    if existing is None:
                        raise RuntimeError("No se encontró el reporte después de una colisión de correlationId.")
                    return _return_idempotent_report(
                        existing,
                        context["sub"],
                        category,
                        description.strip(),
                        municipality_id,
                        latitude,
                        longitude,
                    )
                cursor.execute(
                    """
                    INSERT INTO reporte_historial_estado
                        (reporte_id, estado_previo, estado_nuevo, actor_id, motivo)
                    VALUES (%s, NULL, 'RECIBIDO', %s, 'Reporte creado')
                    """,
                    (report_id, context["sub"]),
                )
                event = create_event(
                    "reporte.creado",
                    report_id,
                    correlation_id,
                    {
                        "actorId": context["sub"],
                        "municipioId": municipality_id,
                        "categoria": category,
                        "lat": latitude,
                        "lon": longitude,
                    },
                )
                enqueue_event(cursor, event)
                return {"report": _report_dict(created)}, 201
    finally:
        connection.close()


@reports.get("/reportes/<report_id>")
@require_roles("ciudadano", "gestor", "admin")
def get_report(report_id):
    report_id = parse_uuid(report_id, "reportId")
    connection = database_connection()
    try:
        with connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute("SELECT * FROM reportes_core WHERE report_id = %s", (report_id,))
            report = cursor.fetchone()
            if report is None:
                raise ApiError(404, "REPORTE_NOT_FOUND", "No se encontró el reporte.")
            context = g.user_context
            if context["role"] == "ciudadano" and report["actor_id"] != context["sub"]:
                raise ApiError(404, "REPORTE_NOT_FOUND", "No se encontró el reporte.")
            if context["role"] == "gestor" and report["municipio_id"] != context.get("municipio_id"):
                raise ApiError(403, "FORBIDDEN", "El reporte pertenece a otro municipio.")
            return {"report": _report_dict(report)}
    finally:
        connection.close()


@reports.get("/reportes")
@require_roles("gestor", "admin")
def list_reports():
    filters = []
    values = []
    context = g.user_context
    if context["role"] == "gestor":
        filters.append("municipio_id = %s")
        values.append(context.get("municipio_id"))
    for query_name, column in (("estado", "estado"), ("categoria", "categoria")):
        value = request.args.get(query_name)
        if value:
            filters.append(f"{column} = %s")
            values.append(value)
    from_date = request.args.get("desde")
    to_date = request.args.get("hasta")
    if from_date:
        filters.append("fecha_creacion >= %s")
        values.append(from_date)
    if to_date:
        filters.append("fecha_creacion < %s::date + interval '1 day'")
        values.append(to_date)

    bbox = request.args.get("bbox")
    if bbox:
        ids = _geospatial_report_ids(bbox)
        if not ids:
            return {"items": [], "nextCursor": None}
        filters.append("report_id = ANY(%s)")
        values.append(ids)

    limit = request.args.get("limit", default=100, type=int)
    if limit < 1 or limit > 500:
        raise ApiError(400, "VALIDATION_ERROR", "limit debe estar entre 1 y 500.")
    cursor = request.args.get("cursor")
    if cursor:
        filters.append("(fecha_creacion, report_id) < (SELECT fecha_creacion, report_id FROM reportes_core WHERE report_id = %s)")
        values.append(parse_uuid(cursor, "cursor"))
    where = f"WHERE {' AND '.join(filters)}" if filters else ""
    connection = database_connection()
    try:
        with connection.cursor(cursor_factory=RealDictCursor) as db_cursor:
            db_cursor.execute(
                f"SELECT * FROM reportes_core {where} ORDER BY fecha_creacion DESC, report_id DESC LIMIT %s",
                (*values, limit + 1),
            )
            rows = db_cursor.fetchall()
        next_cursor = rows[limit - 1]["report_id"] if len(rows) > limit else None
        return {
            "items": [_report_dict(row) for row in rows[:limit]],
            "nextCursor": next_cursor,
        }
    finally:
        connection.close()


def _geospatial_report_ids(bbox):
    parts = bbox.split(",")
    if len(parts) != 4:
        raise ApiError(400, "VALIDATION_ERROR", "bbox debe ser minLon,minLat,maxLon,maxLat.")
    try:
        min_lon, min_lat, max_lon, max_lat = map(float, parts)
    except ValueError:
        raise ApiError(400, "VALIDATION_ERROR", "bbox contiene coordenadas inválidas.")
    if not (-180 <= min_lon < max_lon <= 180 and -90 <= min_lat < max_lat <= 90):
        raise ApiError(400, "VALIDATION_ERROR", "bbox está fuera de rango o invertido.")
    service_url = os.environ.get("GEOSPATIAL_API_URL", "http://service_geospatial:5000")
    try:
        response = requests.get(
            f"{service_url}/geospatial/reportes",
            params={"bbox": bbox},
            headers={"X-User-Context": json.dumps(g.user_context)},
            timeout=2,
        )
    except requests.RequestException as error:
        raise ApiError(503, "GEOSPATIAL_UNAVAILABLE", "El servicio geoespacial no responde.") from error
    if response.status_code >= 400:
        raise ApiError(503, "GEOSPATIAL_UNAVAILABLE", "No fue posible consultar el área geográfica.")
    return [feature["properties"]["reportId"] for feature in response.json().get("features", [])]


@reports.patch("/reportes/<report_id>/estado")
@require_roles("gestor", "admin", "sistema")
def transition_report(report_id):
    report_id = parse_uuid(report_id, "reportId")
    body = require_json_object()
    new_status = body.get("estado")
    reason = body.get("motivo", "")
    if new_status not in TRANSITION_EVENT_TYPES:
        raise ApiError(400, "VALIDATION_ERROR", "estado no es una transición soportada.")
    if not isinstance(reason, str):
        raise ApiError(400, "VALIDATION_ERROR", "motivo debe ser texto.")

    connection = database_connection()
    try:
        with connection:
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute(
                    "SELECT * FROM reportes_core WHERE report_id = %s FOR UPDATE",
                    (report_id,),
                )
                report = cursor.fetchone()
                if report is None:
                    raise ApiError(404, "REPORTE_NOT_FOUND", "No se encontró el reporte.")
                allowed = VALID_TRANSITIONS.get(report["estado"], set())
                if new_status not in allowed:
                    raise ApiError(409, "INVALID_STATE_TRANSITION", "Transición de estado no permitida.")
                actor_id = g.user_context["sub"]
                if g.user_context["role"] == "gestor" and str(report["municipio_id"]) != g.user_context.get("municipio_id"):
                    raise ApiError(403, "FORBIDDEN", "El reporte pertenece a otro municipio.")
                correlation_id = parse_uuid(report["correlation_id"], "correlationId")
                cursor.execute(
                    "UPDATE reportes_core SET estado = %s, actualizado_en = now() WHERE report_id = %s RETURNING *",
                    (new_status, report_id),
                )
                updated = cursor.fetchone()
                cursor.execute(
                    """
                    INSERT INTO reporte_historial_estado
                        (reporte_id, estado_previo, estado_nuevo, actor_id, motivo)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (report_id, report["estado"], new_status, actor_id, reason or None),
                )
                event = create_event(
                    TRANSITION_EVENT_TYPES[new_status],
                    report_id,
                    correlation_id,
                    {
                        "actorId": actor_id,
                        "municipioId": str(report["municipio_id"]),
                        "estadoPrevio": report["estado"],
                        "estadoNuevo": new_status,
                        **({"motivo": reason} if reason else {}),
                    },
                )
                enqueue_event(cursor, event)
                return {"report": _report_dict(updated)}
    finally:
        connection.close()


def _authorize_report_access(report, context):
    if context["role"] == "ciudadano" and str(report["actor_id"]) != context["sub"]:
        raise ApiError(404, "REPORTE_NOT_FOUND", "No se encontró el reporte.")
    if context["role"] == "gestor" and str(report["municipio_id"]) != context.get("municipio_id"):
        raise ApiError(403, "FORBIDDEN", "El reporte pertenece a otro municipio.")


def _matches_media_signature(mime_type, signature):
    if mime_type == "image/jpeg":
        return signature.startswith(b"\xff\xd8\xff")
    if mime_type == "image/png":
        return signature.startswith(b"\x89PNG\r\n\x1a\n")
    if mime_type == "image/webp":
        return len(signature) >= 12 and signature[:4] == b"RIFF" and signature[8:12] == b"WEBP"
    return False


def _storage_client_or_error(for_presigning=False):
    try:
        return s3_client(for_presigning=for_presigning), s3_bucket()
    except RuntimeError as error:
        raise ApiError(503, "STORAGE_UNAVAILABLE", "El almacenamiento multimedia no está configurado.") from error


@reports.post("/reportes/<report_id>/multimedia/upload-url")
@require_roles("ciudadano")
def create_multimedia_upload_url(report_id):
    report_id = parse_uuid(report_id, "reportId")
    body = require_json_object()
    mime_type = body.get("mimeType")
    size_bytes = body.get("sizeBytes")
    if mime_type not in ALLOWED_MIME_TYPES:
        raise ApiError(400, "VALIDATION_ERROR", "mimeType debe ser JPEG, PNG o WebP.")
    if isinstance(size_bytes, bool) or not isinstance(size_bytes, int) or not 1 <= size_bytes <= MAX_UPLOAD_BYTES:
        raise ApiError(400, "VALIDATION_ERROR", f"sizeBytes debe estar entre 1 y {MAX_UPLOAD_BYTES}.")

    context = g.user_context
    connection = database_connection()
    try:
        with connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                "SELECT report_id, actor_id, municipio_id FROM reportes_core WHERE report_id = %s",
                (report_id,),
            )
            report = cursor.fetchone()
        if report is None:
            raise ApiError(404, "REPORTE_NOT_FOUND", "No se encontró el reporte.")
        _authorize_report_access(report, context)
    finally:
        connection.close()

    upload_id = str(uuid.uuid4())
    object_key = f"reports/{report_id}/{upload_id}.{ALLOWED_MIME_TYPES[mime_type]}"
    staging_key = f"uploads/pending/{report_id}/{upload_id}.{ALLOWED_MIME_TYPES[mime_type]}"
    client, bucket = _storage_client_or_error(for_presigning=True)
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=UPLOAD_URL_TTL_SECONDS)
    metadata_headers = {
        "report-id": report_id,
        "actor-id": context["sub"],
        "upload-id": upload_id,
    }
    try:
        signed_post = client.generate_presigned_post(
            Bucket=bucket,
            Key=staging_key,
            Fields={
                "Content-Type": mime_type,
                **{f"x-amz-meta-{key}": value for key, value in metadata_headers.items()},
            },
            Conditions=[
                {"Content-Type": mime_type},
                *[
                    {f"x-amz-meta-{key}": value}
                    for key, value in metadata_headers.items()
                ],
                ["content-length-range", size_bytes, size_bytes],
            ],
            ExpiresIn=UPLOAD_URL_TTL_SECONDS,
        )
    except (BotoCoreError, ClientError) as error:
        raise ApiError(503, "STORAGE_UNAVAILABLE", "No se pudo generar la URL de carga.") from error

    connection = database_connection()
    try:
        with connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO multimedia_uploads
                        (upload_id, reporte_id, actor_id, bucket, object_key, mime_type,
                            staging_key, expected_size_bytes, correlation_id, expires_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        upload_id,
                        report_id,
                        context["sub"],
                        bucket,
                        object_key,
                        mime_type,
                        staging_key,
                        size_bytes,
                        g.correlation_id,
                        expires_at,
                    ),
                )
    finally:
        connection.close()

    return {
        "uploadId": upload_id,
        "objectKey": object_key,
        "uploadUrl": signed_post["url"],
        "method": "POST",
        "formFields": signed_post["fields"],
        "fileField": "file",
        "expiresIn": UPLOAD_URL_TTL_SECONDS,
        "expiresAt": expires_at.isoformat(),
    }, 201


@reports.post("/reportes/<report_id>/multimedia")
@require_roles("ciudadano")
def confirm_multimedia_upload(report_id):
    report_id = parse_uuid(report_id, "reportId")
    body = require_json_object()
    upload_id = parse_uuid(body.get("uploadId"), "uploadId")
    context = g.user_context
    connection = database_connection()
    try:
        with connection:
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute(
                    """
                    SELECT u.*, r.actor_id AS report_actor_id, r.municipio_id,
                           r.lat, r.lon
                    FROM multimedia_uploads u
                    JOIN reportes_core r ON r.report_id = u.reporte_id
                    WHERE u.upload_id = %s AND u.reporte_id = %s
                    FOR UPDATE OF u
                    """,
                    (upload_id, report_id),
                )
                upload = cursor.fetchone()
                if upload is None:
                    raise ApiError(404, "UPLOAD_NOT_FOUND", "No se encontró la carga solicitada.")
                report = {
                    "actor_id": upload["report_actor_id"],
                    "municipio_id": upload["municipio_id"],
                }
                _authorize_report_access(report, context)
                if str(upload["actor_id"]) != context["sub"]:
                    raise ApiError(404, "UPLOAD_NOT_FOUND", "No se encontró la carga solicitada.")
                if upload["status"] == "CONFIRMED":
                    cursor.execute(
                        "SELECT id FROM reporte_multimedia WHERE reporte_id = %s AND object_key = %s",
                        (report_id, upload["object_key"]),
                    )
                    media = cursor.fetchone()
                    return {
                        "uploadId": upload_id,
                        "mediaId": str(media["id"]) if media else None,
                        "status": "CONFIRMED",
                    }, 200
                if upload["expires_at"] < datetime.now(timezone.utc):
                    raise ApiError(410, "UPLOAD_EXPIRED", "La URL de carga expiró.")

                client, _ = _storage_client_or_error()
                try:
                    head = client.head_object(Bucket=upload["bucket"], Key=upload["staging_key"])
                except ClientError as error:
                    code = error.response.get("Error", {}).get("Code", "")
                    if code in {"404", "NoSuchKey", "NotFound"}:
                        raise ApiError(409, "UPLOAD_INCOMPLETE", "El objeto aún no está disponible en S3.") from error
                    raise ApiError(503, "STORAGE_UNAVAILABLE", "No se pudo verificar el objeto en S3.") from error
                except BotoCoreError as error:
                    raise ApiError(503, "STORAGE_UNAVAILABLE", "No se pudo verificar el objeto en S3.") from error

                object_metadata = head.get("Metadata", {})
                if (
                    head.get("ContentType") != upload["mime_type"]
                    or head.get("ContentLength") != upload["expected_size_bytes"]
                    or object_metadata.get("report-id") != report_id
                    or object_metadata.get("actor-id") != context["sub"]
                    or object_metadata.get("upload-id") != upload_id
                ):
                    raise ApiError(400, "UPLOAD_METADATA_MISMATCH", "El objeto no coincide con la carga autorizada.")

                try:
                    object_prefix = client.get_object(
                        Bucket=upload["bucket"],
                        Key=upload["staging_key"],
                        Range="bytes=0-15",
                    )
                    try:
                        signature = object_prefix["Body"].read(16)
                    finally:
                        object_prefix["Body"].close()
                except (BotoCoreError, ClientError) as error:
                    raise ApiError(503, "STORAGE_UNAVAILABLE", "No se pudo validar el contenido del objeto.") from error
                if not _matches_media_signature(upload["mime_type"], signature):
                    raise ApiError(415, "UNSUPPORTED_MEDIA_TYPE", "El contenido no coincide con el MIME declarado.")

                try:
                    client.copy_object(
                        Bucket=upload["bucket"],
                        Key=upload["object_key"],
                        CopySource={
                            "Bucket": upload["bucket"],
                            "Key": upload["staging_key"],
                        },
                        MetadataDirective="COPY",
                        **(
                            {"CopySourceIfMatch": head["ETag"]}
                            if head.get("ETag")
                            else {}
                        ),
                    )
                except (BotoCoreError, ClientError) as error:
                    raise ApiError(503, "STORAGE_UNAVAILABLE", "No se pudo finalizar el objeto multimedia.") from error

                media_id = str(uuid.uuid4())
                cursor.execute(
                    """
                    INSERT INTO reporte_multimedia
                        (id, reporte_id, object_key, mime_type, tamano_bytes)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (reporte_id, object_key) DO NOTHING
                    """,
                    (
                        media_id,
                        report_id,
                        upload["object_key"],
                        upload["mime_type"],
                        head["ContentLength"],
                    ),
                )
                longitude = upload["lon"]
                latitude = upload["lat"]
                data = {
                    "bucket": upload["bucket"],
                    "objectKey": upload["object_key"],
                    "mimeType": upload["mime_type"],
                    "tamanoBytes": head["ContentLength"],
                    "metadata": {
                        "etag": head.get("ETag", "").strip('"'),
                        "uploadId": upload_id,
                    },
                }
                if longitude is not None and latitude is not None:
                    data["location"] = {
                        "longitude": float(longitude),
                        "latitude": float(latitude),
                    }
                event = create_event(
                    "multimedia.upload",
                    report_id,
                    upload["correlation_id"],
                    data,
                )
                enqueue_event(cursor, event)
                cursor.execute(
                    """
                    UPDATE multimedia_uploads
                    SET status = 'CONFIRMED', confirmed_at = now()
                    WHERE upload_id = %s
                    """,
                    (upload_id,),
                )
                confirmed_result = {
                    "uploadId": upload_id,
                    "mediaId": media_id,
                    "objectKey": upload["object_key"],
                    "status": "CONFIRMED",
                }
    finally:
        connection.close()

    try:
        client.delete_object(Bucket=upload["bucket"], Key=upload["staging_key"])
    except (BotoCoreError, ClientError):
        LOGGER.warning("No se pudo borrar el objeto temporal uploadId=%s", upload_id)
    return confirmed_result, 201


@reports.get("/reportes/<report_id>/multimedia/<upload_id>/download-url")
@require_roles("ciudadano", "gestor", "admin")
def create_multimedia_download_url(report_id, upload_id):
    report_id = parse_uuid(report_id, "reportId")
    upload_id = parse_uuid(upload_id, "uploadId")
    connection = database_connection()
    try:
        with connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                """
                SELECT u.*, r.actor_id AS report_actor_id, r.municipio_id
                FROM multimedia_uploads u
                JOIN reportes_core r ON r.report_id = u.reporte_id
                WHERE u.upload_id = %s AND u.reporte_id = %s AND u.status = 'CONFIRMED'
                """,
                (upload_id, report_id),
            )
            upload = cursor.fetchone()
            if upload is None:
                raise ApiError(404, "MEDIA_NOT_FOUND", "No se encontró el archivo.")
            _authorize_report_access(
                {"actor_id": upload["report_actor_id"], "municipio_id": upload["municipio_id"]},
                g.user_context,
            )
    finally:
        connection.close()

    client, _ = _storage_client_or_error(for_presigning=True)
    try:
        url = client.generate_presigned_url(
            "get_object",
            Params={"Bucket": upload["bucket"], "Key": upload["object_key"]},
            ExpiresIn=DOWNLOAD_URL_TTL_SECONDS,
            HttpMethod="GET",
        )
    except (BotoCoreError, ClientError) as error:
        raise ApiError(503, "STORAGE_UNAVAILABLE", "No se pudo generar la URL de descarga.") from error
    return {"downloadUrl": url, "expiresIn": DOWNLOAD_URL_TTL_SECONDS}, 200