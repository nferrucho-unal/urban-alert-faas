import os
import json
import sys
import time
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from flask import Flask, jsonify

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "validar"))

from core_services import common as auth

SUBJECT = "11111111-1111-4111-8111-111111111111"
MUNICIPALITY_ID = "22222222-2222-4222-8222-222222222222"
ISSUER = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_test"
CLIENT_ID = "urban-alert-client"


class FakeCursor:
    def __init__(self, row):
        self.row = row
        self.query = None
        self.params = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def execute(self, query, params=None):
        self.query = query
        self.params = params

    def fetchone(self):
        return self.row


class FakeConnection:
    def __init__(self, row):
        self.cursor_instance = FakeCursor(row)

    def cursor(self):
        return self.cursor_instance

    def close(self):
        pass


class JwkClient:
    def __init__(self, public_key):
        self.public_key = public_key

    def get_signing_key_from_jwt(self, token):
        return SimpleNamespace(key=self.public_key)


class AuthBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.public_key = cls.private_key.public_key()

    def token(self, **overrides):
        claims = {
            "iss": ISSUER,
            "sub": SUBJECT,
            "client_id": CLIENT_ID,
            "token_use": "access",
            "cognito:groups": ["admin"],
            "iat": int(time.time()),
            "exp": int(time.time()) + 300,
        }
        claims.update(overrides)
        return jwt.encode(claims, self.private_key, algorithm="RS256", headers={"kid": "test"})

    def test_cognito_access_token_signature_issuer_and_client_are_checked(self):
        token = self.token()
        with (
            patch.dict(
                os.environ,
                {"COGNITO_ISSUER": ISSUER, "COGNITO_APP_CLIENT_ID": CLIENT_ID},
                clear=False,
            ),
            patch.object(auth, "PyJWKClient", return_value=JwkClient(self.public_key)),
        ):
            claims = auth._decode_cognito_access_token(token)
            self.assertEqual(claims["sub"], SUBJECT)
            with self.assertRaises(jwt.PyJWTError):
                auth._decode_cognito_access_token(self.token(iss=ISSUER + "/wrong"))
            with self.assertRaises(auth.ApiError):
                auth._decode_cognito_access_token(self.token(client_id="other-client"))
            with self.assertRaises(auth.ApiError):
                auth._decode_cognito_access_token(self.token(token_use="refresh"))
            with self.assertRaises(jwt.PyJWTError):
                auth._decode_cognito_access_token(self.token(exp=int(time.time()) - 10))

    def test_database_role_overrides_stale_cognito_group(self):
        row = {"rol": "ciudadano", "municipio_id": uuid.UUID(MUNICIPALITY_ID), "activo": True}
        connection = FakeConnection(row)
        claims = {"sub": SUBJECT, "cognito:groups": ["admin"]}
        with patch.object(auth, "database_connection", return_value=connection):
            context = auth._resolve_authoritative_context(claims)
        self.assertEqual(context["role"], "ciudadano")
        self.assertEqual(context["municipio_id"], MUNICIPALITY_ID)
        self.assertEqual(connection.cursor_instance.params, (SUBJECT,))

    def test_request_pipeline_verifies_jwt_and_uses_database_role_not_headers(self):
        app = Flask("authenticated-request-test")
        auth.install_request_context(app)
        auth.register_error_handlers(app)

        @app.get("/citizen")
        @auth.require_roles("ciudadano")
        def citizen_route():
            from flask import g

            return jsonify({"role": g.user_context["role"]})

        token = self.token()
        db_row = {
            "rol": "ciudadano",
            "municipio_id": uuid.UUID(MUNICIPALITY_ID),
            "activo": True,
        }
        env = {
            "COGNITO_ISSUER": ISSUER,
            "COGNITO_APP_CLIENT_ID": CLIENT_ID,
            "GATEWAY_SHARED_SECRET": "random-secret-at-least-32-chars",
            "ALLOW_TEST_USER_CONTEXT": "false",
        }
        with (
            patch.dict(os.environ, env, clear=False),
            patch.object(auth, "PyJWKClient", return_value=JwkClient(self.public_key)),
            patch.object(auth, "database_connection", return_value=FakeConnection(db_row)),
        ):
            response = app.test_client().get(
                "/citizen",
                headers={
                    "Authorization": f"Bearer {token}",
                    "X-Urban-Gateway-Key": env["GATEWAY_SHARED_SECRET"],
                    "X-User-Context": json.dumps({"sub": SUBJECT, "role": "admin"}),
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["role"], "ciudadano")

    def test_disabled_user_is_denied_even_with_valid_claims(self):
        row = {"rol": "ciudadano", "municipio_id": None, "activo": False}
        with patch.object(auth, "database_connection", return_value=FakeConnection(row)):
            with self.assertRaises(auth.ApiError) as raised:
                auth._resolve_authoritative_context({"sub": SUBJECT})
        self.assertEqual(raised.exception.code, "USER_DISABLED")

    def test_spoofed_context_does_not_authenticate_outside_test_mode(self):
        app = Flask("auth-boundary-test")
        auth.install_request_context(app)
        auth.register_error_handlers(app)

        @app.get("/private")
        @auth.require_roles("admin")
        def private_route():
            return jsonify({"ok": True})

        client = app.test_client()
        forged_context = json.dumps({"sub": SUBJECT, "role": "admin"})
        forged_headers = {
            "X-User-Context": forged_context,
            "X-Urban-Gateway-Key": "random-secret-at-least-32-chars",
        }
        with patch.dict(
            os.environ,
            {
                "ALLOW_TEST_USER_CONTEXT": "false",
                "GATEWAY_SHARED_SECRET": "random-secret-at-least-32-chars",
            },
            clear=False,
        ):
            response = client.get("/private", headers=forged_headers)
        self.assertEqual(response.status_code, 401)

    def test_missing_gateway_secret_is_rejected(self):
        app = Flask("gateway-boundary-test")
        auth.install_request_context(app)
        auth.register_error_handlers(app)

        @app.get("/health")
        def health():
            return jsonify({"ok": True})

        with patch.dict(os.environ, {"GATEWAY_SHARED_SECRET": "random-secret-at-least-32-chars"}, clear=False):
            response = app.test_client().get("/health")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json["error"]["code"], "GATEWAY_REQUIRED")

    def test_missing_gateway_secret_configuration_fails_closed(self):
        app = Flask("missing-gateway-config-test")
        auth.install_request_context(app)
        auth.register_error_handlers(app)

        @app.get("/private")
        def private_route():
            return jsonify({"ok": True})

        with patch.dict(
            os.environ,
            {"GATEWAY_SHARED_SECRET": "", "ALLOW_TEST_USER_CONTEXT": "false"},
            clear=False,
        ):
            response = app.test_client().get("/private")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json["error"]["code"], "GATEWAY_CONFIGURATION_ERROR")


if __name__ == "__main__":
    unittest.main()
