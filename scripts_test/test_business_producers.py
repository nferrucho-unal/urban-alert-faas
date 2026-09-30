import sys
import unittest
import uuid
import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "validar"))

from core_services.app import create_app
from core_services.common import create_event, validate_event
from core_services import outbox_relay
from core_services.geospatial import app as geospatial_app
from core_services import geospatial as geospatial_service
from core_services import reports as reports_service
from core_services import users as users_service
from core_services import works as works_service


ACTOR_ID = "11111111-1111-4111-8111-111111111111"
MUNICIPALITY_ID = "22222222-2222-4222-8222-222222222222"
REPORT_ID = "33333333-3333-4333-8333-333333333333"
CORRELATION_ID = "44444444-4444-4444-8444-444444444444"


def user_context(role="gestor", sub=ACTOR_ID, municipality_id=MUNICIPALITY_ID):
    return {
        "X-User-Context": (
            '{"sub":"'
            + sub
            + '","role":"'
            + role
            + '","municipio_id":"'
            + municipality_id
            + '"}'
        )
    }


class FakeCursor:
    def __init__(
        self,
        events=(),
        existing_report=None,
        user_role=None,
        report_row=None,
        existing_work=None,
    ):
        self.events = events
        self.existing_report = existing_report
        self.user_role = user_role
        self.report_row = report_row
        self.existing_work = existing_work
        self.statements = []
        self.current_row = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def execute(self, query, params=None):
        self.statements.append((query, params))
        if "SELECT id FROM municipios" in query:
            self.current_row = {"id": MUNICIPALITY_ID}
        elif "WHERE correlation_id = %s" in query:
            self.current_row = self.existing_report
        elif "SELECT rol FROM usuarios" in query:
            self.current_row = {"rol": self.user_role} if self.user_role else None
        elif "SELECT * FROM reportes_core WHERE report_id" in query:
            self.current_row = self.report_row
        elif "SELECT obra_id FROM obras" in query:
            self.current_row = self.existing_work
        elif "INSERT INTO reportes_core" in query:
            self.current_row = {
                "report_id": params[0],
                "descripcion": params[1],
                "estado": "RECIBIDO",
                "categoria": params[2],
                "actor_id": ACTOR_ID,
                "municipio_id": MUNICIPALITY_ID,
                "lat": params[6],
                "lon": params[7],
                "fecha_creacion": datetime.now(timezone.utc),
                "actualizado_en": datetime.now(timezone.utc),
            }
        elif "SELECT reporte_id, municipio_id, categoria" in query:
            self.current_row = None

    def fetchall(self):
        return self.events

    def fetchone(self):
        return self.current_row


class FakeConnection:
    def __init__(self, events):
        self.fake_cursor = FakeCursor(events)
        self.committed = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.committed = exc_type is None
        return False

    def cursor(self, *args, **kwargs):
        return self.fake_cursor

    def close(self):
        pass


class FakePublisherChannel:
    def __init__(self):
        self.published = []

    def basic_publish(self, **kwargs):
        self.published.append(kwargs)


class BusinessProducerTests(unittest.TestCase):
    def setUp(self):
        core_app = create_app()
        core_app.config["TESTING"] = True
        self.client = core_app.test_client()
        geospatial_app.config["TESTING"] = True
        self.geo_client = geospatial_app.test_client()

    def test_all_business_producer_events_validate(self):
        events = [
            create_event(
                "reporte.creado",
                REPORT_ID,
                CORRELATION_ID,
                {
                    "actorId": ACTOR_ID,
                    "municipioId": MUNICIPALITY_ID,
                    "categoria": "hueco_via",
                    "lat": 4.65,
                    "lon": -74.05,
                },
            ),
            create_event(
                "reporte.validado",
                REPORT_ID,
                CORRELATION_ID,
                {
                    "actorId": ACTOR_ID,
                    "municipioId": MUNICIPALITY_ID,
                    "estadoPrevio": "RECIBIDO",
                    "estadoNuevo": "VALIDADO",
                },
            ),
            create_event(
                "usuario.rol_cambiado",
                None,
                CORRELATION_ID,
                {
                    "usuarioId": REPORT_ID,
                    "actorId": ACTOR_ID,
                    "rolPrevio": "ciudadano",
                    "rolNuevo": "gestor",
                },
            ),
            create_event(
                "obra.asignada",
                REPORT_ID,
                CORRELATION_ID,
                {
                    "obraId": str(uuid.uuid4()),
                    "actorId": ACTOR_ID,
                    "entidadResponsable": "Secretaria de Infraestructura",
                    "presupuestoAsignado": 1000,
                },
            ),
        ]
        for event in events:
            self.assertEqual(validate_event(event), event)

    def test_report_routes_reject_unauthenticated_and_invalid_input(self):
        response = self.client.post("/reportes", json={})
        self.assertEqual(response.status_code, 401)

        response = self.client.post(
            "/reportes",
            headers=user_context("ciudadano"),
            json={"categoria": "unknown"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json["error"]["code"], "VALIDATION_ERROR")

    def test_user_cannot_change_own_role(self):
        response = self.client.patch(
            f"/usuarios/{ACTOR_ID}/rol",
            headers=user_context("admin"),
            json={"rolNuevo": "ciudadano"},
        )
        self.assertEqual(response.status_code, 403)

    def test_geo_endpoint_rejects_invalid_bbox_before_database_access(self):
        response = self.geo_client.get(
            "/geospatial/reportes?bbox=1,2,3",
            headers=user_context("gestor"),
        )
        self.assertEqual(response.status_code, 400)

    def test_geo_manager_query_is_limited_to_claimed_municipality(self):
        connection = FakeConnection([])
        with patch.object(geospatial_service, "database_connection", return_value=connection):
            response = self.geo_client.get(
                "/geospatial/reportes?bbox=-75,4,-73,5",
                headers=user_context("gestor"),
            )
        self.assertEqual(response.status_code, 200)
        query, params = connection.fake_cursor.statements[0]
        self.assertIn("municipio_id = %s", query)
        self.assertEqual(params[-2], MUNICIPALITY_ID)

    def test_report_creation_writes_contract_event_and_is_idempotent(self):
        report_id = str(uuid.uuid4())
        correlation_id = str(uuid.uuid4())
        existing = {
            "report_id": report_id,
            "descripcion": "Bache en la avenida principal",
            "estado": "RECIBIDO",
            "categoria": "hueco_via",
            "actor_id": ACTOR_ID,
            "municipio_id": MUNICIPALITY_ID,
            "lat": 4.65,
            "lon": -74.05,
            "fecha_creacion": datetime.now(timezone.utc),
            "actualizado_en": datetime.now(timezone.utc),
        }
        first_connection = FakeConnection([])
        first_connection.fake_cursor = FakeCursor()
        second_connection = FakeConnection([])
        second_connection.fake_cursor = FakeCursor(existing_report=existing)
        third_connection = FakeConnection([])
        third_connection.fake_cursor = FakeCursor(existing_report=existing)
        body = {
            "descripcion": "Bache en la avenida principal",
            "categoria": "hueco_via",
            "ubicacion": {"lat": 4.65, "lon": -74.05},
            "municipio_id": MUNICIPALITY_ID,
            "correlationId": correlation_id,
        }
        with patch.object(
            reports_service,
            "database_connection",
            side_effect=[first_connection, second_connection, third_connection],
        ):
            created = self.client.post(
                "/reportes", json=body, headers=user_context("ciudadano")
            )
            repeated = self.client.post(
                "/reportes", json=body, headers=user_context("ciudadano")
            )
            reused_by_other_user = self.client.post(
                "/reportes",
                json=body,
                headers=user_context("ciudadano", sub=REPORT_ID),
            )
        self.assertEqual(created.status_code, 201)
        self.assertEqual(repeated.status_code, 200)
        self.assertEqual(reused_by_other_user.status_code, 409)
        self.assertEqual(
            reused_by_other_user.json["error"]["code"], "IDEMPOTENCY_KEY_REUSED"
        )
        outbox_insert = next(
            params
            for query, params in first_connection.fake_cursor.statements
            if "INSERT INTO outbox_eventos" in query
        )
        event = json.loads(outbox_insert[2])
        self.assertEqual(validate_event(event)["eventType"], "reporte.creado")
        self.assertEqual(sum("INSERT INTO outbox_eventos" in query for query, _ in first_connection.fake_cursor.statements), 1)

    def test_admin_role_change_writes_versioned_event(self):
        connection = FakeConnection([])
        connection.fake_cursor = FakeCursor(user_role="ciudadano")
        with patch.object(users_service, "database_connection", return_value=connection):
            response = self.client.patch(
                f"/usuarios/{REPORT_ID}/rol",
                headers=user_context("admin"),
                json={"rolNuevo": "gestor", "correlationId": CORRELATION_ID},
            )
        self.assertEqual(response.status_code, 200)
        outbox_insert = next(
            params
            for query, params in connection.fake_cursor.statements
            if "INSERT INTO outbox_eventos" in query
        )
        event = json.loads(outbox_insert[2])
        self.assertEqual(validate_event(event)["eventType"], "usuario.rol_cambiado")

    def test_work_assignment_updates_report_and_writes_versioned_event(self):
        report = {
            "report_id": REPORT_ID,
            "estado": "VALIDADO",
            "municipio_id": MUNICIPALITY_ID,
            "correlation_id": CORRELATION_ID,
        }
        connection = FakeConnection([])
        connection.fake_cursor = FakeCursor(report_row=report)
        with patch.object(works_service, "database_connection", return_value=connection):
            response = self.client.post(
                "/obras",
                headers=user_context("gestor"),
                json={
                    "reportId": REPORT_ID,
                    "entidadResponsable": "Secretaria de Infraestructura",
                    "presupuestoAsignado": 1500,
                },
            )
        self.assertEqual(response.status_code, 201)
        outbox_insert = next(
            params
            for query, params in connection.fake_cursor.statements
            if "INSERT INTO outbox_eventos" in query
        )
        event = json.loads(outbox_insert[2])
        self.assertEqual(validate_event(event)["eventType"], "obra.asignada")

    def test_outbox_marks_published_only_after_broker_publish(self):
        event = create_event(
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
        connection = FakeConnection(
            [{"id": "event-id", "tipo": "reporte.creado", "payload": event}]
        )
        channel = FakePublisherChannel()
        with patch.object(outbox_relay, "database_connection", return_value=connection):
            self.assertEqual(outbox_relay.publish_pending(channel), 1)
        self.assertTrue(connection.committed)
        self.assertEqual(channel.published[0]["routing_key"], "reporte.creado")
        self.assertEqual(channel.published[0]["properties"].correlation_id, CORRELATION_ID)
        self.assertIn("UPDATE outbox_eventos", connection.fake_cursor.statements[-1][0])


if __name__ == "__main__":
    unittest.main()