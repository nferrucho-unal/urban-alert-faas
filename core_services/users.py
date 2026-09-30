import re

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

users = Blueprint("users", __name__)
VALID_ROLES = {"ciudadano", "gestor", "admin"}
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _profile(row):
    return {
        "userId": str(row["cognito_sub"]),
        "email": row["email"],
        "displayName": row["display_name"],
        "role": row["rol"],
        "municipioId": str(row["municipio_id"]) if row["municipio_id"] else None,
        "active": row["activo"],
    }


@users.get("/usuarios/me")
@require_roles("ciudadano", "gestor", "admin")
def get_my_profile():
    connection = database_connection()
    try:
        with connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                """
                SELECT cognito_sub, email, display_name, rol, municipio_id, activo
                FROM usuarios WHERE cognito_sub = %s
                """,
                (g.user_context["sub"],),
            )
            row = cursor.fetchone()
            if row is None:
                raise ApiError(404, "USER_NOT_FOUND", "No existe un perfil para el usuario.")
            return {"user": _profile(row)}
    finally:
        connection.close()


@users.put("/usuarios/me")
@require_roles("ciudadano")
def upsert_my_profile():
    body = require_json_object()
    email = body.get("email")
    display_name = body.get("displayName")
    municipality_id = body.get("municipioId")
    if not isinstance(email, str) or not EMAIL_PATTERN.fullmatch(email):
        raise ApiError(400, "VALIDATION_ERROR", "email debe ser válido.")
    if display_name is not None and (
        not isinstance(display_name, str) or not 1 <= len(display_name.strip()) <= 120
    ):
        raise ApiError(400, "VALIDATION_ERROR", "displayName debe tener entre 1 y 120 caracteres.")
    if municipality_id is not None:
        municipality_id = parse_uuid(municipality_id, "municipioId")

    sub = g.user_context["sub"]
    connection = database_connection()
    try:
        with connection:
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                if municipality_id:
                    cursor.execute(
                        "SELECT id FROM municipios WHERE id = %s AND activo",
                        (municipality_id,),
                    )
                    if cursor.fetchone() is None:
                        raise ApiError(422, "MUNICIPIO_NOT_FOUND", "El municipio no existe o está inactivo.")
                cursor.execute(
                    """
                    INSERT INTO usuarios
                        (username, rol, cognito_sub, email, display_name, municipio_id, activo)
                    VALUES (%s, %s, %s, %s, %s, %s, TRUE)
                    ON CONFLICT (cognito_sub) DO UPDATE SET
                        email = EXCLUDED.email,
                        display_name = EXCLUDED.display_name,
                        municipio_id = EXCLUDED.municipio_id,
                        actualizado_en = now()
                    RETURNING cognito_sub, email, display_name, rol, municipio_id, activo
                    """,
                    (
                        sub,
                        "ciudadano",
                        sub,
                        email,
                        display_name.strip() if display_name else None,
                        municipality_id,
                    ),
                )
                return {"user": _profile(cursor.fetchone())}, 200
    finally:
        connection.close()


@users.patch("/usuarios/<user_id>/rol")
@require_roles("admin")
def change_user_role(user_id):
    user_id = parse_uuid(user_id, "userId")
    actor_id = g.user_context["sub"]
    if user_id == actor_id:
        raise ApiError(403, "FORBIDDEN", "Un administrador no puede cambiar su propio rol.")
    body = require_json_object()
    new_role = body.get("rolNuevo")
    if new_role not in VALID_ROLES:
        raise ApiError(400, "VALIDATION_ERROR", "rolNuevo no es un rol permitido.")

    connection = database_connection()
    try:
        with connection:
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute(
                    "SELECT rol FROM usuarios WHERE cognito_sub = %s AND activo FOR UPDATE",
                    (user_id,),
                )
                current = cursor.fetchone()
                if current is None:
                    raise ApiError(404, "USER_NOT_FOUND", "No existe el usuario activo.")
                previous_role = current["rol"]
                if previous_role == new_role:
                    return {"userId": user_id, "role": new_role, "changed": False}
                correlation_id = parse_uuid(body.get("correlationId", g.correlation_id), "correlationId")
                cursor.execute(
                    "UPDATE usuarios SET rol = %s, actualizado_en = now() WHERE cognito_sub = %s",
                    (new_role, user_id),
                )
                event = create_event(
                    "usuario.rol_cambiado",
                    None,
                    correlation_id,
                    {
                        "usuarioId": user_id,
                        "actorId": actor_id,
                        "rolPrevio": previous_role,
                        "rolNuevo": new_role,
                    },
                )
                enqueue_event(cursor, event)
                return {"userId": user_id, "role": new_role, "changed": True}
    finally:
        connection.close()