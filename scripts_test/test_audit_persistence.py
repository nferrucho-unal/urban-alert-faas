import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "validar"))
sys.path.insert(0, str(PROJECT_ROOT / "scripts_test"))

from eventos import evento_reporte_creado
from fn_audit import app as audit


class FakeCursor:
    def __init__(self, last_hash, already_stored=False):
        self.last_hash = last_hash
        self.already_stored = already_stored
        self.statements = []
        self.result = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def execute(self, query, params=None):
        self.statements.append((query, params))
        if "SELECT last_hash" in query:
            self.result = (self.last_hash,)
        elif "SELECT event_id FROM audit_events" in query:
            self.result = (params[0],) if self.already_stored else None

    def fetchone(self):
        return self.result


class FakeConnection:
    def __init__(self, last_hash="0" * 64, already_stored=False):
        self.fake_cursor = FakeCursor(last_hash, already_stored)
        self.committed = False
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.committed = exc_type is None
        return False

    def cursor(self):
        return self.fake_cursor

    def close(self):
        self.closed = True


class FakeChannel:
    def __init__(self):
        self.actions = []

    def basic_ack(self, delivery_tag):
        self.actions.append(("ack", delivery_tag))

    def basic_nack(self, delivery_tag, requeue):
        self.actions.append(("nack", delivery_tag, requeue))

    def basic_reject(self, delivery_tag, requeue):
        self.actions.append(("reject", delivery_tag, requeue))

    def basic_publish(self, **kwargs):
        self.actions.append(("publish", kwargs))


class AuditPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.event = evento_reporte_creado()
        self.body = json.dumps(self.event).encode("utf-8")
        self.method = SimpleNamespace(delivery_tag=12)
        self.properties = SimpleNamespace(correlation_id=self.event["correlationId"])

    def test_event_is_committed_before_ack_and_survives_consumer_restart(self):
        first_connection = FakeConnection()
        first_channel = FakeChannel()
        with patch.object(audit, "database_connection", return_value=first_connection):
            audit.callback_auditoria(first_channel, self.method, self.properties, self.body)

        self.assertTrue(first_connection.committed)
        self.assertTrue(first_connection.closed)
        self.assertEqual(first_channel.actions, [("ack", 12)])
        insert = next(
            params
            for query, params in first_connection.fake_cursor.statements
            if "INSERT INTO audit_events" in query
        )
        self.assertEqual(insert[0], self.event["eventId"])
        self.assertEqual(len(insert[8]), 64)
        self.assertEqual(len(insert[9]), 64)

        restarted_connection = FakeConnection(
            last_hash=insert[9], already_stored=True
        )
        restarted_channel = FakeChannel()
        with patch.object(
            audit, "database_connection", return_value=restarted_connection
        ):
            audit.callback_auditoria(
                restarted_channel, self.method, self.properties, self.body
            )

        self.assertEqual(restarted_channel.actions, [("ack", 12)])
        self.assertFalse(
            any(
                "INSERT INTO audit_events" in query
                for query, _ in restarted_connection.fake_cursor.statements
            )
        )

    def test_database_failure_does_not_ack_event(self):
        channel = FakeChannel()
        with patch.object(audit, "database_connection", side_effect=RuntimeError("db offline")):
            audit.callback_auditoria(channel, self.method, self.properties, self.body)
        self.assertEqual(channel.actions, [("nack", 12, True)])


if __name__ == "__main__":
    unittest.main()
