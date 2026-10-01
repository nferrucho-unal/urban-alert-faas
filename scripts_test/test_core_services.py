import json
import os
import sys
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from botocore.exceptions import ClientError

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "validar"))

from core_services.app import create_app
from core_services import create_dev_bucket, geospatial, geospatial_projector, outbox_relay, storage, users
from eventos import evento_reporte_creado

ACTOR_ID = "11111111-1111-4111-8111-111111111111"
MUNICIPALITY_ID = "22222222-2222-4222-8222-222222222222"
REPORT_ID = "33333333-3333-4333-8333-333333333333"
CORRELATION_ID = "44444444-4444-4444-8444-444444444444"


def user_headers(role="ciudadano"):
    return {
        "X-User-Context": json.dumps(
            {"sub": ACTOR_ID, "role": role, "municipio_id": MUNICIPALITY_ID}
        )
    }


class FakeCursor:
    def __init__(self, fetchone_results=(), fetchall_results=()):
        self.fetchone_results = list(fetchone_results)
        self.fetchall_results = fetchall_results
        self.current_row = None
        self.statements = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def execute(self, query, params=None):
        self.statements.append((query, params))
        self.current_row = (
            self.fetchone_results.pop(0) if self.fetchone_results else None
        )

    def fetchone(self):
        return self.current_row

    def fetchall(self):
        return self.fetchall_results


class FakeConnection:
    def __init__(self, cursor):
        self.fake_cursor = cursor
        self.committed = False
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.committed = exc_type is None
        return False

    def cursor(self, *args, **kwargs):
        return self.fake_cursor

    def close(self):
        self.closed = True


class FakeRedis:
    def __init__(self, existing=(), attempts=0):
        self.keys = set(existing)
        self.attempts = attempts
        self.values = {}

    def exists(self, key):
        return key in self.keys

    def set(self, key, value, ex=None):
        self.keys.add(key)
        self.values[key] = value
        return True

    def delete(self, key):
        self.keys.discard(key)

    def incr(self, key):
        self.attempts += 1
        self.values[key] = str(self.attempts)
        return self.attempts

    def expire(self, key, seconds):
        return True


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


class CoreServiceComponentTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()
        geospatial.app.config["TESTING"] = True

    def test_core_health_endpoint(self):
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["service"], "urban-alert-core")

    def test_storage_requires_bucket_and_uses_presign_endpoint(self):
        with patch.dict(os.environ, {"S3_BUCKET": "urban-alert-test"}, clear=False):
            self.assertEqual(storage.s3_bucket(), "urban-alert-test")
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "S3_BUCKET"):
                storage.s3_bucket()

        environment = {
            "AWS_REGION": "us-east-1",
            "S3_ENDPOINT_URL": "http://s3.internal:8333",
            "S3_PRESIGN_ENDPOINT_URL": "http://localhost:8333",
            "S3_ACCESS_KEY_ID": "test-access",
            "S3_SECRET_ACCESS_KEY": "test-secret",
        }
        with patch.dict(os.environ, environment, clear=False), patch.object(
            storage.boto3, "client", return_value=object()
        ) as create_client:
            storage.s3_client(for_presigning=True)
        kwargs = create_client.call_args.kwargs
        self.assertEqual(kwargs["endpoint_url"], "http://localhost:8333")
        self.assertEqual(kwargs["aws_access_key_id"], "test-access")
        self.assertEqual(kwargs["aws_secret_access_key"], "test-secret")
        self.assertEqual(kwargs["region_name"], "us-east-1")

    def test_dev_bucket_bootstrap_creates_bucket_after_not_found(self):
        client = unittest.mock.Mock()
        client.head_bucket.side_effect = ClientError(
            {"Error": {"Code": "404"}}, "HeadBucket"
        )
        with (
            patch.object(create_dev_bucket, "s3_client", return_value=client),
            patch.object(create_dev_bucket, "s3_bucket", return_value="local-bucket"),
            patch.object(create_dev_bucket.time, "sleep"),
        ):
            create_dev_bucket.main()
        client.create_bucket.assert_called_once_with(Bucket="local-bucket")

    def test_dev_bucket_bootstrap_is_idempotent_when_bucket_exists(self):
        client = unittest.mock.Mock()
        with (
            patch.object(create_dev_bucket, "s3_client", return_value=client),
            patch.object(create_dev_bucket, "s3_bucket", return_value="local-bucket"),
        ):
            create_dev_bucket.main()
        client.head_bucket.assert_called_once_with(Bucket="local-bucket")
        client.create_bucket.assert_not_called()

    def test_user_profile_read_uses_current_subject(self):
        row = {
            "cognito_sub": uuid.UUID(ACTOR_ID),
            "email": "citizen@example.test",
            "display_name": "Citizen",
            "rol": "ciudadano",
            "municipio_id": uuid.UUID(MUNICIPALITY_ID),
            "activo": True,
        }
        connection = FakeConnection(FakeCursor(fetchone_results=[row]))
        with patch.object(users, "database_connection", return_value=connection):
            response = self.client.get("/usuarios/me", headers=user_headers())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["user"]["userId"], ACTOR_ID)
        self.assertEqual(connection.fake_cursor.statements[0][1], (ACTOR_ID,))
        self.assertTrue(connection.closed)

    def test_user_profile_upsert_validates_municipality_and_returns_profile(self):
        row = {
            "cognito_sub": uuid.UUID(ACTOR_ID),
            "email": "citizen@example.test",
            "display_name": "Citizen",
            "rol": "ciudadano",
            "municipio_id": uuid.UUID(MUNICIPALITY_ID),
            "activo": True,
        }
        cursor = FakeCursor(
            fetchone_results=[{"id": uuid.UUID(MUNICIPALITY_ID)}, row]
        )
        connection = FakeConnection(cursor)
        with patch.object(users, "database_connection", return_value=connection):
            response = self.client.put(
                "/usuarios/me",
                headers=user_headers(),
                json={
                    "email": "citizen@example.test",
                    "displayName": "Citizen",
                    "municipioId": MUNICIPALITY_ID,
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["user"]["email"], "citizen@example.test")
        self.assertTrue(any("INSERT INTO usuarios" in query for query, _ in cursor.statements))
        self.assertTrue(connection.committed)

    def test_geospatial_endpoint_returns_geojson_feature(self):
        row = {
            "reporte_id": uuid.UUID(REPORT_ID),
            "municipio_id": uuid.UUID(MUNICIPALITY_ID),
            "categoria": "alumbrado",
            "estado": "RECIBIDO",
            "creado_en": datetime.now(timezone.utc),
            "lon": -74.05,
            "lat": 4.65,
        }
        connection = FakeConnection(FakeCursor(fetchall_results=[row]))
        with patch.object(geospatial, "database_connection", return_value=connection):
            response = geospatial.app.test_client().get(
                "/geospatial/reportes?bbox=-75,4,-73,5",
                headers=user_headers("gestor"),
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["type"], "FeatureCollection")
        feature = response.json["features"][0]
        self.assertEqual(feature["geometry"]["coordinates"], [-74.05, 4.65])
        self.assertEqual(feature["properties"]["reportId"], REPORT_ID)
        self.assertTrue(connection.closed)

    def test_projector_projects_event_and_marks_redis_idempotency_key(self):
        event = evento_reporte_creado(
            report_id=REPORT_ID,
            event_id="55555555-5555-4555-8555-555555555555",
            correlation_id=CORRELATION_ID,
            actor_id=ACTOR_ID,
        )
        redis_client = FakeRedis()
        channel = FakeChannel()
        method = SimpleNamespace(delivery_tag=12)
        properties = SimpleNamespace(correlation_id=CORRELATION_ID)
        with (
            patch.object(geospatial_projector.redis.Redis, "from_url", return_value=redis_client),
            patch.object(geospatial_projector, "project_event") as project,
        ):
            geospatial_projector.callback_projector(
                channel,
                method,
                properties,
                json.dumps(event).encode("utf-8"),
            )
        self.assertEqual(channel.actions, [("ack", 12)])
        project.assert_called_once_with(event)
        self.assertIn(f"geospatial:projection:{event['eventId']}", redis_client.keys)

    def test_projector_acks_duplicate_without_reprojecting(self):
        event = evento_reporte_creado(
            report_id=REPORT_ID,
            event_id="55555555-5555-4555-8555-555555555555",
            correlation_id=CORRELATION_ID,
            actor_id=ACTOR_ID,
        )
        key = f"geospatial:projection:{event['eventId']}"
        channel = FakeChannel()
        with (
            patch.object(
                geospatial_projector.redis.Redis,
                "from_url",
                return_value=FakeRedis(existing=[key]),
            ),
            patch.object(geospatial_projector, "project_event") as project,
        ):
            geospatial_projector.callback_projector(
                channel,
                SimpleNamespace(delivery_tag=13),
                SimpleNamespace(correlation_id=CORRELATION_ID),
                json.dumps(event).encode("utf-8"),
            )
        self.assertEqual(channel.actions, [("ack", 13)])
        project.assert_not_called()

    def test_projector_requeues_transient_failure_before_retry_limit(self):
        event = evento_reporte_creado(
            report_id=REPORT_ID,
            event_id="55555555-5555-4555-8555-555555555555",
            correlation_id=CORRELATION_ID,
            actor_id=ACTOR_ID,
        )
        channel = FakeChannel()
        with (
            patch.object(
                geospatial_projector.redis.Redis,
                "from_url",
                return_value=FakeRedis(attempts=0),
            ),
            patch.object(
                geospatial_projector,
                "project_event",
                side_effect=RuntimeError("postgis unavailable"),
            ),
        ):
            geospatial_projector.callback_projector(
                channel,
                SimpleNamespace(delivery_tag=14),
                SimpleNamespace(correlation_id=CORRELATION_ID),
                json.dumps(event).encode("utf-8"),
            )
        self.assertEqual(channel.actions, [("nack", 14, True)])

    def test_projector_dead_letters_failure_at_retry_limit(self):
        event = evento_reporte_creado(
            report_id=REPORT_ID,
            event_id="55555555-5555-4555-8555-555555555555",
            correlation_id=CORRELATION_ID,
            actor_id=ACTOR_ID,
        )
        channel = FakeChannel()
        properties = SimpleNamespace(
            headers=None,
            content_type="application/json",
            content_encoding=None,
            correlation_id=CORRELATION_ID,
            message_id=event["eventId"],
            timestamp=None,
            type="reporte.creado",
        )
        with (
            patch.object(
                geospatial_projector.redis.Redis,
                "from_url",
                return_value=FakeRedis(attempts=4),
            ),
            patch.object(
                geospatial_projector,
                "project_event",
                side_effect=RuntimeError("postgis unavailable"),
            ),
        ):
            geospatial_projector.callback_projector(
                channel,
                SimpleNamespace(delivery_tag=15),
                properties,
                json.dumps(event).encode("utf-8"),
            )
        self.assertEqual(channel.actions[0][0], "publish")
        self.assertEqual(channel.actions[0][1]["exchange"], "urban_alert_dlx")
        self.assertEqual(
            channel.actions[0][1]["routing_key"], "geospatial.processing_failed"
        )
        self.assertEqual(channel.actions[-1], ("ack", 15))

    def test_outbox_keeps_event_unpublished_when_broker_publish_fails(self):
        event = evento_reporte_creado(
            report_id=REPORT_ID,
            event_id="55555555-5555-4555-8555-555555555555",
            correlation_id=CORRELATION_ID,
            actor_id=ACTOR_ID,
        )
        cursor = FakeCursor(
            fetchall_results=[
                {"id": event["eventId"], "tipo": event["eventType"], "payload": event}
            ]
        )
        connection = FakeConnection(cursor)
        channel = FakeChannel()
        channel.basic_publish = unittest.mock.Mock(side_effect=RuntimeError("broker down"))
        with patch.object(outbox_relay, "database_connection", return_value=connection):
            with self.assertRaisesRegex(RuntimeError, "broker down"):
                outbox_relay.publish_pending(channel)
        self.assertFalse(connection.committed)
        self.assertFalse(
            any("UPDATE outbox_eventos" in query for query, _ in cursor.statements)
        )
        self.assertTrue(connection.closed)


if __name__ == "__main__":
    unittest.main()
