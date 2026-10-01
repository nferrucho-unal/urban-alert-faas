import sys
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "validar"))

from core_services.app import create_app
from core_services import audit_api as audit_service
from core_services.audit_integrity import GENESIS_HASH, calcular_hash_evento
from core_services.common import create_event

ACTOR_ID = "11111111-1111-4111-8111-111111111111"
MUNICIPALITY_ID = "22222222-2222-4222-8222-222222222222"
REPORT_ID = "33333333-3333-4333-8333-333333333333"
CORRELATION_ID = "44444444-4444-4444-8444-444444444444"


def context_headers(role="ciudadano", subject=ACTOR_ID, municipality=MUNICIPALITY_ID):
    return {
        "X-User-Context": (
            f'{{"sub":"{subject}","role":"{role}",'
            f'"municipio_id":"{municipality}"}}'
        )
    }


class FakeCursor:
    def __init__(self, report=None, events=None, chain_state=None):
        self.report = report
        self.events = events or []
        self.chain_state = chain_state
        self.current = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def execute(self, query, params=None):
        if "SELECT actor_id, municipio_id FROM reportes_core" in query:
            self.current = self.report
        elif "FROM audit_events" in query:
            self.current = self.events
        elif "FROM audit_chain_state" in query:
            self.current = self.chain_state
        else:
            self.current = None

    def fetchone(self):
        return self.current

    def fetchall(self):
        return self.current


class FakeConnection:
    def __init__(self, cursor):
        self.fake_cursor = cursor

    def cursor(self, *args, **kwargs):
        return self.fake_cursor

    def close(self):
        pass


class AuditApiTests(unittest.TestCase):
    def setUp(self):
        app = create_app()
        app.config["TESTING"] = True
        self.client = app.test_client()
        self.report = {
            "actor_id": ACTOR_ID,
            "municipio_id": uuid.UUID(MUNICIPALITY_ID),
        }
        self.event = create_event(
            "reporte.creado",
            REPORT_ID,
            CORRELATION_ID,
            {
                "actorId": ACTOR_ID,
                "municipioId": MUNICIPALITY_ID,
                "categoria": "otro",
                "lat": 4.65,
                "lon": -74.05,
            },
        )
        event_hash = calcular_hash_evento(self.event, GENESIS_HASH)
        self.row = {
            "sequence_id": 1,
            "event_id": uuid.UUID(self.event["eventId"]),
            "event_type": "reporte.creado",
            "event_version": 1,
            "occurred_at": datetime.now(timezone.utc),
            "actor_id": uuid.UUID(ACTOR_ID),
            "correlation_id": uuid.UUID(CORRELATION_ID),
            "payload": self.event,
            "previous_hash": GENESIS_HASH,
            "record_hash": event_hash,
        }
        self.read_store = [self.row]
        self.chain_state = {"last_hash": event_hash}

    def make_second_row(self):
        event = create_event(
            "reporte.validado",
            REPORT_ID,
            CORRELATION_ID,
            {
                "actorId": ACTOR_ID,
                "municipioId": MUNICIPALITY_ID,
                "estadoPrevio": "RECIBIDO",
                "estadoNuevo": "VALIDADO",
            },
        )
        record_hash = calcular_hash_evento(event, self.row["record_hash"])
        return {
            **self.row,
            "sequence_id": 2,
            "event_id": uuid.UUID(event["eventId"]),
            "event_type": "reporte.validado",
            "payload": event,
            "previous_hash": self.row["record_hash"],
            "record_hash": record_hash,
        }

    def mock_databases(self):
        core = FakeConnection(FakeCursor(report=self.report))
        audit = FakeConnection(
            FakeCursor(
                report=None,
                events=self.read_store,
                chain_state=self.chain_state,
            )
        )
        return core, audit

    def test_timeline_is_paginated_and_authorized(self):
        second_row = self.make_second_row()
        self.read_store = [self.row, second_row]
        self.chain_state = {"last_hash": second_row["record_hash"]}
        core, audit = self.mock_databases()
        with (
            patch.object(audit_service, "database_connection", return_value=core),
            patch.object(audit_service, "audit_read_connection", return_value=audit),
        ):
            response = self.client.get(
                f"/auditoria/reportes/{REPORT_ID}?limit=1",
                headers=context_headers("ciudadano"),
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["reportId"], REPORT_ID)
        self.assertEqual(response.json["items"][0]["eventType"], "reporte.creado")
        self.assertEqual(response.json["nextCursor"], "1")

        audit = FakeConnection(
            FakeCursor(events=[second_row], chain_state=self.chain_state)
        )
        with (
            patch.object(audit_service, "database_connection", return_value=core),
            patch.object(audit_service, "audit_read_connection", return_value=audit),
        ):
            next_page = self.client.get(
                f"/auditoria/reportes/{REPORT_ID}?limit=1&cursor=1",
                headers=context_headers("ciudadano"),
            )
        self.assertEqual(next_page.status_code, 200)
        self.assertEqual(next_page.json["items"][0]["eventType"], "reporte.validado")
        self.assertIsNone(next_page.json["nextCursor"])

    def test_timeline_hides_reports_owned_by_other_citizens(self):
        core, audit = self.mock_databases()
        self.report["actor_id"] = "55555555-5555-4555-8555-555555555555"
        with (
            patch.object(audit_service, "database_connection", return_value=core),
            patch.object(audit_service, "audit_read_connection", return_value=audit) as read_store,
        ):
            response = self.client.get(
                f"/auditoria/reportes/{REPORT_ID}",
                headers=context_headers("ciudadano"),
            )
        self.assertEqual(response.status_code, 404)
        read_store.assert_not_called()

    def test_manager_cannot_read_reports_from_another_municipality(self):
        core, audit = self.mock_databases()
        self.report["municipio_id"] = uuid.UUID("55555555-5555-4555-8555-555555555555")
        with (
            patch.object(audit_service, "database_connection", return_value=core),
            patch.object(audit_service, "audit_read_connection", return_value=audit) as read_store,
        ):
            response = self.client.get(
                f"/auditoria/reportes/{REPORT_ID}",
                headers=context_headers("gestor"),
            )
        self.assertEqual(response.status_code, 403)
        read_store.assert_not_called()

    def test_integrity_endpoint_detects_hash_chain_mutation(self):
        self.read_store = [dict(self.row, record_hash="f" * 64)]
        core, audit = self.mock_databases()
        with (
            patch.object(audit_service, "database_connection", return_value=core),
            patch.object(audit_service, "audit_read_connection", return_value=audit),
        ):
            response = self.client.get(
                f"/auditoria/reportes/{REPORT_ID}/integridad",
                headers=context_headers("admin"),
            )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json["verified"])
        self.assertEqual(response.json["brokenSequence"], 1)

    def test_integrity_endpoint_accepts_valid_chain(self):
        core, audit = self.mock_databases()
        with (
            patch.object(audit_service, "database_connection", return_value=core),
            patch.object(audit_service, "audit_read_connection", return_value=audit),
        ):
            response = self.client.get(
                f"/auditoria/reportes/{REPORT_ID}/integridad",
                headers=context_headers("gestor"),
            )
        self.assertTrue(response.json["verified"])
        self.assertEqual(response.json["reportEventCount"], 1)


if __name__ == "__main__":
    unittest.main()
