import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "validar"))

from eventos import evento_multimedia_upload, evento_reporte_creado
from fn_audit import app as audit
from fn_multimedia import app as multimedia
from fn_notifications import app as notifications


class FakeChannel:
    def __init__(self):
        self.actions = []

    def basic_ack(self, delivery_tag):
        self.actions.append(("ack", delivery_tag))

    def basic_reject(self, delivery_tag, requeue):
        self.actions.append(("reject", delivery_tag, requeue))

    def basic_nack(self, delivery_tag, requeue):
        self.actions.append(("nack", delivery_tag, requeue))

    def basic_publish(self, exchange, routing_key, body, properties, mandatory):
        self.actions.append(
            ("publish", exchange, routing_key, body, properties, mandatory)
        )


class FakeRedis:
    def __init__(self):
        self.values = {}

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True


class ConsumerContractTests(unittest.TestCase):
    def setUp(self):
        self.method = SimpleNamespace(delivery_tag=7)
        self.properties = SimpleNamespace(correlation_id=None)

    def invoke(self, callback, event):
        channel = FakeChannel()
        callback(
            channel,
            self.method,
            self.properties,
            json.dumps(event).encode("utf-8"),
        )
        return channel.actions

    def test_audit_valid_invalid_and_unknown_version(self):
        valid_events = [
            evento_reporte_creado(),
            json.loads(
                (
                    PROJECT_ROOT
                    / "validar"
                    / "ejemplos"
                    / "reporte.validado.v1.example.json"
                ).read_text(encoding="utf-8")
            ),
            json.loads(
                (
                    PROJECT_ROOT
                    / "validar"
                    / "ejemplos"
                    / "obra.asignada.v1.example.json"
                ).read_text(encoding="utf-8")
            ),
            json.loads(
                (
                    PROJECT_ROOT
                    / "validar"
                    / "ejemplos"
                    / "usuario.rol_cambiado.v1.example.json"
                ).read_text(encoding="utf-8")
            ),
        ]
        with patch.object(
            audit,
            "persistir_evento_auditoria",
            side_effect=lambda event: {
                "eventId": event["eventId"],
                "reportId": event["reportId"],
                "evento": event["eventType"],
                "actorId": event["data"]["actorId"],
                "correlationId": event["correlationId"],
                "occurredAt": event["occurredAt"],
                "hash_anterior": "0" * 64,
                "hash": "1" * 64,
            },
        ) as persist_event:
            for event in valid_events:
                self.assertEqual(
                    self.invoke(audit.callback_auditoria, event), [("ack", 7)]
                )
        self.assertEqual(persist_event.call_count, len(valid_events))

        invalid = dict(valid_events[0])
        invalid.pop("data")
        actions = self.invoke(audit.callback_auditoria, invalid)
        self.assertEqual(actions[0][0:3], ("publish", "urban_alert_dlx", "auditoria.invalid"))
        self.assertEqual(actions[-1], ("ack", 7))

        unsupported = dict(valid_events[0], version=2)
        self.assertEqual(
            self.invoke(audit.callback_auditoria, unsupported), [("ack", 7)]
        )

    def test_notifications_valid_invalid_and_unknown_version(self):
        valid_events = [
            evento_reporte_creado(),
            json.loads(
                (
                    PROJECT_ROOT
                    / "validar"
                    / "ejemplos"
                    / "obra.asignada.v1.example.json"
                ).read_text(encoding="utf-8")
            ),
        ]
        with patch.object(notifications, "redis_client", FakeRedis()), patch.object(
            notifications, "enviar_notificacion", return_value=None
        ):
            for event in valid_events:
                self.assertEqual(
                    self.invoke(notifications.callback_notificaciones_con_dlq, event),
                    [("ack", 7)],
                )

        valid = valid_events[0]
        invalid = dict(valid)
        invalid.pop("data")
        self.assertEqual(
            self.invoke(notifications.callback_notificaciones_con_dlq, invalid),
            [("reject", 7, False)],
        )

        unsupported = dict(valid, version=2)
        self.assertEqual(
            self.invoke(notifications.callback_notificaciones_con_dlq, unsupported),
            [("ack", 7)],
        )

    def test_multimedia_valid_invalid_and_unknown_version(self):
        valid = evento_multimedia_upload(
            report_id=evento_reporte_creado()["reportId"],
            bucket="test-bucket",
            object_key="reports/test/photo.jpg",
            metadata={"format": "JPEG"},
            location={"longitude": -74.05, "latitude": 4.65},
        )
        with patch.object(multimedia, "persistir_metadatos") as persist:
            self.assertEqual(
                self.invoke(multimedia.callback_multimedia, valid), [("ack", 7)]
            )
            persisted = persist.call_args.args[0]
            self.assertEqual(persisted["mime_type"], "image/jpeg")
            self.assertEqual(persisted["metadata"], {"format": "JPEG"})

        invalid = dict(valid)
        invalid.pop("data")
        actions = self.invoke(multimedia.callback_multimedia, invalid)
        self.assertEqual(
            actions[0][0:3], ("publish", "urban_alert_dlx", "multimedia.invalid")
        )
        self.assertEqual(actions[-1], ("ack", 7))

        unsupported = dict(valid, version=2)
        self.assertEqual(
            self.invoke(multimedia.callback_multimedia, unsupported), [("ack", 7)]
        )


if __name__ == "__main__":
    unittest.main()
