import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from validar.event_contracts import validate_event

MUNICIPIO_ID_PRUEBAS = "22222222-2222-4222-8222-222222222222"


def evento_reporte_creado(
    report_id=None,
    event_id=None,
    correlation_id=None,
    actor_id=None,
    municipio_id=MUNICIPIO_ID_PRUEBAS,
):
    event_id = event_id or str(uuid.uuid4())
    event = {
        "eventId": event_id,
        "eventType": "reporte.creado",
        "version": 1,
        "occurredAt": datetime.now(timezone.utc).isoformat(),
        "correlationId": correlation_id or str(uuid.uuid4()),
        "reportId": report_id or str(uuid.uuid4()),
        "data": {
            "actorId": actor_id or str(uuid.uuid4()),
            "municipioId": municipio_id,
            "categoria": "otro",
            "lat": 4.65,
            "lon": -74.05,
        },
    }
    return validate_event(event, "reporte.creado")


def evento_multimedia_upload(
    report_id,
    bucket,
    object_key,
    correlation_id=None,
    mime_type="image/jpeg",
    tamano_bytes=123456,
    metadata=None,
    location=None,
):
    data = {
        "bucket": bucket,
        "objectKey": object_key,
        "mimeType": mime_type,
        "tamanoBytes": tamano_bytes,
    }
    if metadata is not None:
        data["metadata"] = metadata
    if location is not None:
        data["location"] = location

    event = {
        "eventId": str(uuid.uuid4()),
        "eventType": "multimedia.upload",
        "version": 1,
        "occurredAt": datetime.now(timezone.utc).isoformat(),
        "correlationId": correlation_id or str(uuid.uuid4()),
        "reportId": report_id,
        "data": data,
    }
    return validate_event(event, "multimedia.upload")
