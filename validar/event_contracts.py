import json
import re
from functools import lru_cache
from pathlib import Path

import pika
from jsonschema import Draft202012Validator, FormatChecker

SCHEMAS_DIR = Path(__file__).resolve().parent / "schemas"
EVENT_TYPE_PATTERN = re.compile(r"^[a-z]+\.[a-z_]+$")


class ContractValidationError(ValueError):
    pass


class UnsupportedEventError(ContractValidationError):
    pass


@lru_cache(maxsize=64)
def _get_validator(event_type: str, version: int) -> Draft202012Validator:
    schema_path = SCHEMAS_DIR / f"{event_type}.v{version}.schema.json"
    if not schema_path.is_file():
        raise UnsupportedEventError(
            f"No hay esquema para eventType={event_type!r}, version={version!r}"
        )

    with schema_path.open(encoding="utf-8") as schema_file:
        schema = json.load(schema_file)
    return Draft202012Validator(schema, format_checker=FormatChecker())


def validate_event(event: object, expected_event_type: str | None = None) -> dict:
    if not isinstance(event, dict):
        raise ContractValidationError("El evento debe ser un objeto JSON")

    event_type = event.get("eventType")
    version = event.get("version")
    if (
        not isinstance(event_type, str)
        or EVENT_TYPE_PATTERN.fullmatch(event_type) is None
        or not isinstance(version, int)
        or isinstance(version, bool)
        or version < 1
    ):
        raise ContractValidationError("eventType o version no válidos")

    if expected_event_type is not None and event_type != expected_event_type:
        raise ContractValidationError(
            f"Se esperaba eventType={expected_event_type!r}, se recibió {event_type!r}"
        )

    validator = _get_validator(event_type, version)
    errors = sorted(
        validator.iter_errors(event),
        key=lambda error: tuple(str(part) for part in error.path),
    )
    if errors:
        details = "; ".join(
            f"{'/'.join(str(part) for part in error.path) or '(raíz)'}: {error.message}"
            for error in errors
        )
        raise ContractValidationError(details)

    return event


def dead_letter_invalid_message(
    channel, method, properties, body: bytes, routing_key: str, reason: str
) -> bool:
    headers = dict(getattr(properties, "headers", None) or {})
    headers["contract-validation-error"] = reason[:1000]
    dead_letter_properties = pika.BasicProperties(
        content_type=getattr(properties, "content_type", None) or "application/json",
        content_encoding=getattr(properties, "content_encoding", None),
        headers=headers,
        delivery_mode=2,
        correlation_id=getattr(properties, "correlation_id", None),
        message_id=getattr(properties, "message_id", None),
        timestamp=getattr(properties, "timestamp", None),
        type=getattr(properties, "type", None),
    )

    try:
        channel.basic_publish(
            exchange="urban_alert_dlx",
            routing_key=routing_key,
            body=body,
            properties=dead_letter_properties,
            mandatory=True,
        )
    except Exception:
        channel.basic_nack(delivery_tag=method.delivery_tag, requeue=True)
        return False

    channel.basic_ack(delivery_tag=method.delivery_tag)
    return True
