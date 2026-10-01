import os
from datetime import datetime, timezone

import psycopg2
from flask import Blueprint, g, request
from psycopg2.extras import RealDictCursor

from core_services.audit_integrity import GENESIS_HASH, calcular_hash_evento
from core_services.common import ApiError, database_connection, parse_uuid, require_roles


audit_api = Blueprint("audit_api", __name__)


def audit_read_connection():
    dsn = os.environ.get("AUDIT_READ_DATABASE_URL")
    if not dsn:
        raise RuntimeError("Falta configurar AUDIT_READ_DATABASE_URL.")
    return psycopg2.connect(dsn, cursor_factory=RealDictCursor)


def _authorize_report(report_id):
    connection = database_connection()
    try:
        with connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                "SELECT actor_id, municipio_id FROM reportes_core WHERE report_id = %s",
                (report_id,),
            )
            report = cursor.fetchone()
    finally:
        connection.close()

    if report is None:
        raise ApiError(404, "REPORTE_NOT_FOUND", "No se encontró el reporte.")
    context = g.user_context
    if context["role"] == "ciudadano" and str(report["actor_id"]) != context["sub"]:
        raise ApiError(404, "REPORTE_NOT_FOUND", "No se encontró el reporte.")
    if context["role"] == "gestor" and str(report["municipio_id"]) != context.get("municipio_id"):
        raise ApiError(403, "FORBIDDEN", "El reporte pertenece a otro municipio.")


def _event_response(row):
    payload = row["payload"]
    return {
        "sequence": row["sequence_id"],
        "eventId": str(row["event_id"]),
        "eventType": row["event_type"],
        "version": row["event_version"],
        "occurredAt": row["occurred_at"].isoformat(),
        "actorId": str(row["actor_id"]),
        "correlationId": str(row["correlation_id"]),
        "data": payload["data"],
        "previousHash": row["previous_hash"].strip(),
        "hash": row["record_hash"].strip(),
    }


@audit_api.get("/auditoria/reportes/<report_id>")
@require_roles("ciudadano", "gestor", "admin")
def reporte_historial(report_id):
    report_id = parse_uuid(report_id, "reportId")
    _authorize_report(report_id)
    limit = request.args.get("limit", default=100, type=int)
    if limit < 1 or limit > 500:
        raise ApiError(400, "VALIDATION_ERROR", "limit debe estar entre 1 y 500.")
    cursor_value = request.args.get("cursor", "0")
    if not cursor_value.isdigit():
        raise ApiError(400, "VALIDATION_ERROR", "cursor debe ser un entero de secuencia.")
    cursor_sequence = int(cursor_value)

    connection = audit_read_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT sequence_id, event_id, event_type, event_version, occurred_at,
                       actor_id, correlation_id, payload, previous_hash, record_hash
                FROM audit_events
                WHERE report_id = %s AND sequence_id > %s
                ORDER BY sequence_id ASC
                LIMIT %s
                """,
                (report_id, cursor_sequence, limit + 1),
            )
            rows = cursor.fetchall()
        next_cursor = str(rows[limit - 1]["sequence_id"]) if len(rows) > limit else None
        return {
            "reportId": report_id,
            "items": [_event_response(row) for row in rows[:limit]],
            "nextCursor": next_cursor,
        }
    finally:
        connection.close()


@audit_api.get("/auditoria/reportes/<report_id>/integridad")
@require_roles("gestor", "admin")
def verificar_integridad_reporte(report_id):
    report_id = parse_uuid(report_id, "reportId")
    _authorize_report(report_id)
    connection = audit_read_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT sequence_id, event_id, payload, previous_hash, record_hash
                FROM audit_events
                ORDER BY sequence_id ASC
                """
            )
            rows = cursor.fetchall()
            cursor.execute(
                "SELECT last_hash, last_event_id FROM audit_chain_state WHERE singleton = TRUE"
            )
            chain_state = cursor.fetchone()
    finally:
        connection.close()

    expected_previous = GENESIS_HASH
    broken_sequence = None
    report_events = 0
    for row in rows:
        stored_previous = row["previous_hash"].strip()
        stored_hash = row["record_hash"].strip()
        payload = row["payload"]
        calculated_hash = calcular_hash_evento(payload, expected_previous)
        if stored_previous != expected_previous or stored_hash != calculated_hash:
            broken_sequence = row["sequence_id"]
            break
        expected_previous = stored_hash
        if str(payload.get("reportId")) == report_id:
            report_events += 1

    state_hash = chain_state["last_hash"].strip() if chain_state else None
    head_matches = state_hash == expected_previous
    verified = broken_sequence is None and head_matches
    return {
        "reportId": report_id,
        "verified": verified,
        "reportEventCount": report_events,
        "chainEventCount": len(rows),
        "headMatches": head_matches,
        "brokenSequence": broken_sequence,
        "checkedAt": datetime.now(timezone.utc).isoformat(),
    }, 200


if __name__ == "__main__":
    raise SystemExit("Audit query API is registered by core_services.app")
