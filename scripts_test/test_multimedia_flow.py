import sys
import unittest
import uuid
from io import BytesIO
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "validar"))

from core_services.app import create_app
from core_services import reports as reports_service
from validar.event_contracts import validate_event

ACTOR_ID = "11111111-1111-4111-8111-111111111111"
MUNICIPALITY_ID = "22222222-2222-4222-8222-222222222222"
REPORT_ID = "33333333-3333-4333-8333-333333333333"
CORRELATION_ID = "44444444-4444-4444-8444-444444444444"


def citizen_headers():
    return {
        "X-User-Context": (
            f'{{"sub":"{ACTOR_ID}","role":"ciudadano",'
            f'"municipio_id":"{MUNICIPALITY_ID}"}}'
        )
    }


class FakeCursor:
    def __init__(self, report=None, upload=None):
        self.report = report
        self.upload = upload
        self.current_row = None
        self.statements = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def execute(self, query, params=None):
        self.statements.append((query, params))
        if "SELECT report_id, actor_id, municipio_id" in query:
            self.current_row = self.report
        elif "FROM multimedia_uploads u" in query:
            self.current_row = self.upload
        else:
            self.current_row = None

    def fetchone(self):
        return self.current_row


class FakeConnection:
    def __init__(self, report=None, upload=None):
        self.fake_cursor = FakeCursor(report=report, upload=upload)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def cursor(self, *args, **kwargs):
        return self.fake_cursor

    def close(self):
        pass


class FakeS3:
    def __init__(self, head=None, content=b"\xff\xd8\xff\xdb"):
        self.head = head
        self.content = content
        self.post = None
        self.get = None
        self.copied = None
        self.deleted = None

    def generate_presigned_post(self, **kwargs):
        self.post = kwargs
        return {"url": "http://minio/upload", "fields": {"key": kwargs["Key"], "policy": "signed"}}

    def head_object(self, **kwargs):
        return self.head

    def get_object(self, **kwargs):
        return {"Body": BytesIO(self.content)}

    def copy_object(self, **kwargs):
        self.copied = kwargs

    def delete_object(self, **kwargs):
        self.deleted = kwargs

    def generate_presigned_url(self, operation, **kwargs):
        self.get = (operation, kwargs)
        return "http://minio/download"


class MultimediaFlowTests(unittest.TestCase):
    def setUp(self):
        app = create_app()
        app.config["TESTING"] = True
        self.client = app.test_client()
        self.report = {
            "report_id": REPORT_ID,
            "actor_id": ACTOR_ID,
            "municipio_id": MUNICIPALITY_ID,
            "lat": 4.65,
            "lon": -74.05,
        }

    def test_upload_url_has_scoped_key_and_exact_size_policy(self):
        lookup = FakeConnection(report=self.report)
        insert = FakeConnection()
        s3 = FakeS3()
        with (
            patch.object(reports_service, "database_connection", side_effect=[lookup, insert]),
            patch.object(reports_service, "s3_client", return_value=s3),
            patch.object(reports_service, "s3_bucket", return_value="private-evidence"),
        ):
            response = self.client.post(
                f"/reportes/{REPORT_ID}/multimedia/upload-url",
                headers=citizen_headers(),
                json={"mimeType": "image/jpeg", "sizeBytes": 2048},
            )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json["method"], "POST")
        self.assertTrue(response.json["objectKey"].startswith(f"reports/{REPORT_ID}/"))
        self.assertIn(["content-length-range", 2048, 2048], s3.post["Conditions"])
        self.assertTrue(s3.post["Key"].startswith(f"uploads/pending/{REPORT_ID}/"))
        self.assertEqual(s3.post["Fields"]["x-amz-meta-actor-id"], ACTOR_ID)
        self.assertTrue(
            any("INSERT INTO multimedia_uploads" in query for query, _ in insert.fake_cursor.statements)
        )

    def test_confirmation_verifies_s3_and_writes_multimedia_event_to_outbox(self):
        upload_id = str(uuid.uuid4())
        object_key = f"reports/{REPORT_ID}/{upload_id}.jpg"
        upload = {
            "upload_id": upload_id,
            "reporte_id": REPORT_ID,
            "actor_id": ACTOR_ID,
            "report_actor_id": ACTOR_ID,
            "municipio_id": MUNICIPALITY_ID,
            "bucket": "private-evidence",
            "object_key": object_key,
            "staging_key": f"uploads/pending/{REPORT_ID}/{upload_id}.jpg",
            "mime_type": "image/jpeg",
            "expected_size_bytes": 2048,
            "correlation_id": CORRELATION_ID,
            "status": "PENDING",
            "expires_at": datetime.now(timezone.utc) + timedelta(minutes=3),
            "lat": 4.65,
            "lon": -74.05,
        }
        connection = FakeConnection(upload=upload)
        s3 = FakeS3(
            head={
                "ContentType": "image/jpeg",
                "ContentLength": 2048,
                "Metadata": {
                    "report-id": REPORT_ID,
                    "actor-id": ACTOR_ID,
                    "upload-id": upload_id,
                },
                "ETag": '"etag-1"',
            }
        )
        with (
            patch.object(reports_service, "database_connection", return_value=connection),
            patch.object(reports_service, "s3_client", return_value=s3),
            patch.object(reports_service, "s3_bucket", return_value="private-evidence"),
        ):
            response = self.client.post(
                f"/reportes/{REPORT_ID}/multimedia",
                headers=citizen_headers(),
                json={"uploadId": upload_id},
            )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(s3.copied["Key"], object_key)
        self.assertEqual(s3.deleted["Key"], upload["staging_key"])
        outbox_params = next(
            params
            for query, params in connection.fake_cursor.statements
            if "INSERT INTO outbox_eventos" in query
        )
        event = validate_event(__import__("json").loads(outbox_params[2]))
        self.assertEqual(event["eventType"], "multimedia.upload")
        self.assertEqual(event["data"]["objectKey"], object_key)
        self.assertEqual(event["data"]["tamanoBytes"], 2048)
        self.assertEqual(event["data"]["location"]["longitude"], -74.05)

    def test_confirm_rejects_object_with_wrong_metadata(self):
        upload_id = str(uuid.uuid4())
        upload = {
            "upload_id": upload_id,
            "reporte_id": REPORT_ID,
            "actor_id": ACTOR_ID,
            "report_actor_id": ACTOR_ID,
            "municipio_id": MUNICIPALITY_ID,
            "bucket": "private-evidence",
            "object_key": f"reports/{REPORT_ID}/{upload_id}.jpg",
            "staging_key": f"uploads/pending/{REPORT_ID}/{upload_id}.jpg",
            "mime_type": "image/jpeg",
            "expected_size_bytes": 2048,
            "correlation_id": CORRELATION_ID,
            "status": "PENDING",
            "expires_at": datetime.now(timezone.utc) + timedelta(minutes=3),
            "lat": 4.65,
            "lon": -74.05,
        }
        connection = FakeConnection(upload=upload)
        s3 = FakeS3(
            head={
                "ContentType": "image/jpeg",
                "ContentLength": 2048,
                "Metadata": {"report-id": "other-report"},
            }
        )
        with (
            patch.object(reports_service, "database_connection", return_value=connection),
            patch.object(reports_service, "s3_client", return_value=s3),
            patch.object(reports_service, "s3_bucket", return_value="private-evidence"),
        ):
            response = self.client.post(
                f"/reportes/{REPORT_ID}/multimedia",
                headers=citizen_headers(),
                json={"uploadId": upload_id},
            )

        self.assertEqual(response.status_code, 400)
        self.assertIsNone(s3.copied)
        self.assertFalse(
            any("INSERT INTO outbox_eventos" in query for query, _ in connection.fake_cursor.statements)
        )

    def test_confirm_rejects_declared_image_with_non_image_content(self):
        upload_id = str(uuid.uuid4())
        upload = {
            "upload_id": upload_id,
            "reporte_id": REPORT_ID,
            "actor_id": ACTOR_ID,
            "report_actor_id": ACTOR_ID,
            "municipio_id": MUNICIPALITY_ID,
            "bucket": "private-evidence",
            "object_key": f"reports/{REPORT_ID}/{upload_id}.jpg",
            "staging_key": f"uploads/pending/{REPORT_ID}/{upload_id}.jpg",
            "mime_type": "image/jpeg",
            "expected_size_bytes": 12,
            "correlation_id": CORRELATION_ID,
            "status": "PENDING",
            "expires_at": datetime.now(timezone.utc) + timedelta(minutes=3),
            "lat": 4.65,
            "lon": -74.05,
        }
        connection = FakeConnection(upload=upload)
        s3 = FakeS3(
            head={
                "ContentType": "image/jpeg",
                "ContentLength": 12,
                "Metadata": {
                    "report-id": REPORT_ID,
                    "actor-id": ACTOR_ID,
                    "upload-id": upload_id,
                },
            },
            content=b"not an image",
        )
        with (
            patch.object(reports_service, "database_connection", return_value=connection),
            patch.object(reports_service, "s3_client", return_value=s3),
            patch.object(reports_service, "s3_bucket", return_value="private-evidence"),
        ):
            response = self.client.post(
                f"/reportes/{REPORT_ID}/multimedia",
                headers=citizen_headers(),
                json={"uploadId": upload_id},
            )

        self.assertEqual(response.status_code, 415)
        self.assertIsNone(s3.copied)
        self.assertFalse(
            any("INSERT INTO outbox_eventos" in query for query, _ in connection.fake_cursor.statements)
        )

    def test_download_url_is_signed_and_private(self):
        upload_id = str(uuid.uuid4())
        upload = {
            "upload_id": upload_id,
            "reporte_id": REPORT_ID,
            "actor_id": ACTOR_ID,
            "report_actor_id": ACTOR_ID,
            "municipio_id": MUNICIPALITY_ID,
            "bucket": "private-evidence",
            "object_key": f"reports/{REPORT_ID}/{upload_id}.jpg",
            "status": "CONFIRMED",
        }
        connection = FakeConnection(upload=upload)
        s3 = FakeS3()
        with (
            patch.object(reports_service, "database_connection", return_value=connection),
            patch.object(reports_service, "s3_client", return_value=s3),
            patch.object(reports_service, "s3_bucket", return_value="private-evidence"),
        ):
            response = self.client.get(
                f"/reportes/{REPORT_ID}/multimedia/{upload_id}/download-url",
                headers=citizen_headers(),
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["downloadUrl"], "http://minio/download")
        self.assertEqual(s3.get[0], "get_object")


if __name__ == "__main__":
    unittest.main()
