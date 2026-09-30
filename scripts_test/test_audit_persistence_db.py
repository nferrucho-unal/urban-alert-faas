import json
import os
import sys
from pathlib import Path

import psycopg2

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "validar"))

try:
    from fn_audit.app import persistir_evento_auditoria
except ModuleNotFoundError:
    from app import persistir_evento_auditoria
from validar.event_contracts import validate_event


EVENT = validate_event(
    {
        "eventId": "77777777-7777-4777-8777-777777777777",
        "eventType": "reporte.creado",
        "version": 1,
        "occurredAt": "2026-09-30T00:00:00Z",
        "correlationId": "88888888-8888-4888-8888-888888888888",
        "reportId": "99999999-9999-4999-8999-999999999999",
        "data": {
            "actorId": "11111111-1111-4111-8111-111111111111",
            "municipioId": "22222222-2222-4222-8222-222222222222",
            "categoria": "otro",
            "lat": 4.65,
            "lon": -74.05,
        },
    },
    "reporte.creado",
)


def main():
    dsn = os.environ.get("AUDIT_DATABASE_URL")
    if not dsn:
        raise RuntimeError("Ejecuta este test con AUDIT_DATABASE_URL configurado.")

    first_result = persistir_evento_auditoria(EVENT)
    replay_result = persistir_evento_auditoria(EVENT)
    if replay_result is not None:
        raise AssertionError("Un eventId repetido no debe insertarse dos veces.")

    with psycopg2.connect(dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT payload, previous_hash, record_hash FROM audit_events WHERE event_id = %s",
                (EVENT["eventId"],),
            )
            stored = cursor.fetchone()
            if stored is None:
                raise AssertionError("El evento no quedó persistido en PostgreSQL.")
            cursor.execute(
                "SELECT count(*) FROM audit_events WHERE event_id = %s",
                (EVENT["eventId"],),
            )
            count = cursor.fetchone()[0]

    payload = stored[0]
    if isinstance(payload, str):
        payload = json.loads(payload)
    assert payload["eventId"] == EVENT["eventId"]
    assert len(stored[1]) == 64
    assert len(stored[2]) == 64
    assert count == 1
    print(
        f"[OK] durable eventId={EVENT['eventId']} "
        f"inserted={first_result is not None} replay_inserted={replay_result is not None} "
        f"rows={count} hash={stored[2]}"
    )


if __name__ == "__main__":
    main()
