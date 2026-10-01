import json
import sys
import unittest
from botocore.exceptions import ClientError
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
    def __init__(self, last_hash, existing_record=None):
        self.last_hash = last_hash
        self.existing_record = existing_record
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
        elif "SELECT payload, previous_hash, record_hash" in query:
            self.result = self.existing_record

    def fetchone(self):
        return self.result


class FakeConnection:
    def __init__(self, last_hash="0" * 64, existing_record=None):
        self.fake_cursor = FakeCursor(last_hash, existing_record)
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
            last_hash=insert[9],
            existing_record=(json.dumps(self.event), "0" * 64, insert[9]),
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

    def test_rejected_and_resolved_contract_examples_are_audited(self):
        event_types = ("reporte.rechazado", "reporte.resuelto")
        for event_type in event_types:
            with self.subTest(event_type=event_type):
                example_path = (
                    PROJECT_ROOT
                    / "validar"
                    / "ejemplos"
                    / f"{event_type}.v1.example.json"
                )
                event = json.loads(example_path.read_text(encoding="utf-8"))
                connection = FakeConnection()
                channel = FakeChannel()
                properties = SimpleNamespace(correlation_id=event["correlationId"])
                with (
                    patch.object(audit, "database_connection", return_value=connection),
                    patch.object(audit, "archivar_evento_inmutable"),
                ):
                    audit.callback_auditoria(
                        channel,
                        self.method,
                        properties,
                        json.dumps(event).encode("utf-8"),
                    )

                self.assertTrue(connection.committed)
                self.assertEqual(channel.actions, [("ack", 12)])
                insert = next(
                    params
                    for query, params in connection.fake_cursor.statements
                    if "INSERT INTO audit_events" in query
                )
                self.assertEqual(insert[1], event_type)
                self.assertEqual(insert[0], event["eventId"])
                self.assertEqual(len(insert[8]), 64)
                self.assertEqual(len(insert[9]), 64)

    def test_archive_uses_s3_compliance_object_lock_before_ack(self):
        s3 = unittest.mock.Mock()
        s3.head_object.side_effect = ClientError(
            {"Error": {"Code": "404"}, "ResponseMetadata": {"HTTPStatusCode": 404}},
            "HeadObject",
        )
        record = audit._audit_record(self.event, "0" * 64, "a" * 64)
        with (
            patch.object(audit, "AUDIT_ARCHIVE_BUCKET", "urban-alert-audit"),
            patch.object(audit.boto3, "client", return_value=s3),
        ):
            audit.archivar_evento_inmutable(record)

        s3.put_object.assert_called_once()
        put_args = s3.put_object.call_args.kwargs
        self.assertEqual(put_args["Bucket"], "urban-alert-audit")
        self.assertEqual(put_args["ObjectLockMode"], "COMPLIANCE")
        self.assertIsNotNone(put_args["ObjectLockRetainUntilDate"].tzinfo)


if __name__ == "__main__":
    unittest.main()
