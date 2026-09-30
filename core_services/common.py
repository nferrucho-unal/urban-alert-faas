import json
import hmac
import os
import uuid
from datetime import datetime, timezone
from functools import wraps
from http import HTTPStatus

import jwt
import psycopg2
from flask import current_app, g, jsonify, request
from jwt import PyJWKClient
from jwt.exceptions import PyJWTError
from werkzeug.exceptions import HTTPException
from psycopg2.extras import RealDictCursor

from validar.event_contracts import validate_event


class ApiError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def _cognito_issuer():
    issuer = os.environ.get("COGNITO_ISSUER")
    if issuer:
        return issuer.rstrip("/")
    user_pool_id = os.environ.get("COGNITO_USER_POOL_ID")
    region = os.environ.get("AWS_REGION")
    if user_pool_id and region:
        return f"https://cognito-idp.{region}.amazonaws.com/{user_pool_id}"
    return None


def _decode_cognito_access_token(token):
    issuer = _cognito_issuer()
    app_client_id = os.environ.get("COGNITO_APP_CLIENT_ID")
    if not issuer or not app_client_id:
        raise ApiError(503, "AUTH_CONFIGURATION_ERROR", "La validación de identidad no está configurada.")

    jwks_client = PyJWKClient(f"{issuer}/.well-known/jwks.json", cache_keys=True)
    signing_key = jwks_client.get_signing_key_from_jwt(token)
    claims = jwt.decode(
        token,
        signing_key.key,
        algorithms=["RS256"],
        issuer=issuer,
        options={"verify_aud": False},
    )
    token_use = claims.get("token_use")
    valid_client = (
        claims.get("client_id") == app_client_id
        if token_use == "access"
        else claims.get("aud") == app_client_id
        if token_use == "id"
        else False
    )
    if not valid_client:
        raise ApiError(401, "UNAUTHENTICATED", "Se requiere un access token válido de Urban Alert.")
    return claims


def _role_from_claims(claims):
    groups = set(claims.get("cognito:groups", []))
    if "admin" in groups:
        return "admin"
    if "gestor" in groups:
        return "gestor"
    if "sistema" in groups:
        return "sistema"
    return "ciudadano"


def _resolve_authoritative_context(claims):
    subject = parse_uuid(claims.get("sub"), "sub")
    role = _role_from_claims(claims)
    municipality_id = claims.get("custom:municipio_id") or claims.get("municipio_id")
    if municipality_id:
        municipality_id = parse_uuid(municipality_id, "municipio_id")

    connection = database_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT rol, municipio_id, activo FROM usuarios WHERE cognito_sub = %s",
                (subject,),
            )
            user = cursor.fetchone()
    finally:
        connection.close()

    if user:
        if not user["activo"]:
            raise ApiError(403, "USER_DISABLED", "La cuenta está desactivada.")
        role = str(user["rol"]).lower()
        municipality_id = (
            str(user["municipio_id"]) if user["municipio_id"] is not None else municipality_id
        )
    if role not in {"ciudadano", "gestor", "admin", "sistema"}:
        raise ApiError(403, "FORBIDDEN", "El usuario no tiene un rol habilitado.")
    return {"sub": subject, "role": role, "municipio_id": municipality_id}


def register_error_handlers(app):
    @app.errorhandler(ApiError)
    def handle_api_error(error):
        correlation_id = getattr(g, "correlation_id", None)
        return jsonify(
            {
                "error": {
                    "code": error.code,
                    "message": error.message,
                    "correlationId": correlation_id,
                }
            }
        ), error.status

    @app.errorhandler(Exception)
    def handle_unexpected_error(error):
        if isinstance(error, HTTPException):
            return jsonify(
                {
                    "error": {
                        "code": HTTPStatus(error.code).phrase.upper().replace(" ", "_"),
                        "message": error.description,
                        "correlationId": getattr(g, "correlation_id", None),
                    }
                }
            ), error.code
        app.logger.exception("Unhandled service error")
        return jsonify(
            {
                "error": {
                    "code": "INTERNAL_ERROR",
                    "message": "Ocurrió un error interno.",
                    "correlationId": getattr(g, "correlation_id", None),
                }
            }
        ), 500


def install_request_context(app):
    @app.before_request
    def load_request_context():
        correlation_id = request.headers.get("X-Correlation-Id")
        try:
            g.correlation_id = str(uuid.UUID(correlation_id)) if correlation_id else str(uuid.uuid4())
        except (ValueError, AttributeError):
            raise ApiError(400, "VALIDATION_ERROR", "X-Correlation-Id debe ser UUID.")

        test_context_allowed = (
            current_app.testing
            or os.environ.get("ALLOW_TEST_USER_CONTEXT", "false").lower() == "true"
        )
        gateway_secret = os.environ.get("GATEWAY_SHARED_SECRET")
        if not gateway_secret and not test_context_allowed:
            raise ApiError(
                503,
                "GATEWAY_CONFIGURATION_ERROR",
                "La clave de API Gateway no está configurada.",
            )
        if gateway_secret and not hmac.compare_digest(
            request.headers.get("X-Urban-Gateway-Key", ""), gateway_secret
        ):
            raise ApiError(403, "GATEWAY_REQUIRED", "La solicitud debe ingresar por API Gateway.")

        if request.endpoint == "health":
            g.user_context = None
            return

        raw_context = request.headers.get("X-User-Context")
        if raw_context and test_context_allowed:
            try:
                context = json.loads(raw_context)
                if not isinstance(context, dict):
                    raise ValueError("context must be an object")
                context["sub"] = parse_uuid(context.get("sub"), "sub")
                context["role"] = str(context["role"]).lower()
                if context["role"] not in {"ciudadano", "gestor", "admin", "sistema"}:
                    raise ValueError("unsupported role")
                if context.get("municipio_id"):
                    context["municipio_id"] = parse_uuid(context["municipio_id"], "municipio_id")
                g.user_context = context
                return
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                raise ApiError(401, "UNAUTHENTICATED", "Contexto de prueba inválido.")

        authorization = request.headers.get("Authorization", "")
        scheme, separator, token = authorization.partition(" ")
        if not separator or scheme.lower() != "bearer" or not token:
            raise ApiError(401, "UNAUTHENTICATED", "Se requiere un access token Bearer.")
        try:
            claims = _decode_cognito_access_token(token)
            g.user_context = _resolve_authoritative_context(claims)
        except (PyJWTError, ValueError, TypeError, KeyError) as error:
            raise ApiError(401, "UNAUTHENTICATED", "El access token es inválido o expiró.") from error


def require_roles(*roles):
    def decorate(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            context = getattr(g, "user_context", None)
            if context is None:
                raise ApiError(401, "UNAUTHENTICATED", "Se requiere un usuario autenticado.")
            if context["role"] not in roles:
                raise ApiError(403, "FORBIDDEN", "No tiene permiso para esta operación.")
            return view(*args, **kwargs)

        return wrapped

    return decorate


def database_connection(geo=False):
    variable = "DATABASE_GEO_URL" if geo else "DATABASE_PRIMARY_URL"
    dsn = os.environ.get(variable) or os.environ.get("DATABASE_URL")
    if not dsn:
        raise RuntimeError(f"Falta configurar {variable}.")
    return psycopg2.connect(dsn, cursor_factory=RealDictCursor)


def parse_uuid(value, field):
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        raise ApiError(400, "VALIDATION_ERROR", f"{field} debe ser un UUID válido.")


def require_json_object():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        raise ApiError(400, "VALIDATION_ERROR", "El cuerpo debe ser un objeto JSON.")
    return body


def create_event(event_type, report_id, correlation_id, data):
    event = {
        "eventId": str(uuid.uuid4()),
        "eventType": event_type,
        "version": 1,
        "occurredAt": datetime.now(timezone.utc).isoformat(),
        "correlationId": parse_uuid(correlation_id, "correlationId"),
        "reportId": str(report_id) if report_id is not None else None,
        "data": data,
    }
    return validate_event(event, event_type)


def enqueue_event(cursor, event):
    cursor.execute(
        """
        INSERT INTO outbox_eventos (id, tipo, payload, correlation_id)
        VALUES (%s, %s, %s::jsonb, %s)
        """,
        (
            event["eventId"],
            event["eventType"],
            json.dumps(event, separators=(",", ":")),
            event["correlationId"],
        ),
    )


def api_response(payload, status=200):
    return jsonify(payload), status