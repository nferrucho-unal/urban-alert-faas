import uuid

from flask import Blueprint, g
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

works = Blueprint("works", __name__)


@works.post("/obras")
@require_roles("gestor", "admin")
def assign_work():
    body = require_json_object()
    report_id = parse_uuid(body.get("reportId"), "reportId")
    organization = body.get("entidadResponsable")
    budget = body.get("presupuestoAsignado")
    if not isinstance(organization, str) or not 1 <= len(organization.strip()) <= 100:
        raise ApiError(400, "VALIDATION_ERROR", "entidadResponsable es obligatoria (1-100 caracteres).")
    if budget is not None and (
        isinstance(budget, bool) or not isinstance(budget, (int, float)) or budget < 0
    ):
        raise ApiError(400, "VALIDATION_ERROR", "presupuestoAsignado debe ser un número no negativo.")

    actor_id = g.user_context["sub"]
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
                if g.user_context["role"] == "gestor" and str(report["municipio_id"]) != g.user_context.get("municipio_id"):
                    raise ApiError(403, "FORBIDDEN", "El reporte pertenece a otro municipio.")
                if report["estado"] != "VALIDADO":
                    raise ApiError(409, "INVALID_STATE_TRANSITION", "Solo se pueden asignar reportes validados.")

                cursor.execute("SELECT obra_id FROM obras WHERE report_id = %s", (report_id,))
                existing = cursor.fetchone()
                if existing:
                    return {"obraId": str(existing["obra_id"]), "reportId": report_id}, 200

                work_id = str(uuid.uuid4())
                cursor.execute(
                    """
                    INSERT INTO obras (obra_id, report_id, entidad_responsable, presupuesto_asignado)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (work_id, report_id, organization.strip(), budget),
                )
                cursor.execute(
                    "UPDATE reportes_core SET estado = 'EN_OBRA', actualizado_en = now() WHERE report_id = %s",
                    (report_id,),
                )
                cursor.execute(
                    """
                    INSERT INTO reporte_historial_estado
                        (reporte_id, estado_previo, estado_nuevo, actor_id, motivo)
                    VALUES (%s, 'VALIDADO', 'EN_OBRA', %s, 'Obra asignada')
                    """,
                    (report_id, actor_id),
                )
                event = create_event(
                    "obra.asignada",
                    report_id,
                    report["correlation_id"],
                    {
                        "obraId": work_id,
                        "actorId": actor_id,
                        "entidadResponsable": organization.strip(),
                        **({"presupuestoAsignado": budget} if budget is not None else {}),
                    },
                )
                enqueue_event(cursor, event)
                return {
                    "obraId": work_id,
                    "reportId": report_id,
                    "estado": "EN_OBRA",
                }, 201
    finally:
        connection.close()