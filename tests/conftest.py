import base64
from typing import Any

import httpx
import pytest

from ec_nightly.config import Config

BLUEAPI = "http://p99-blueapi.p99-beamline.svc.cluster.local:80"
TOKEN_URL = "https://identity.example.org/realms/dls/protocol/openid-connect/token"
CLIENT_ID = "ec-nightly-p99"
CLIENT_SECRET = "fake-secret-never-logged"  # gitleaks:allow (test fixture)
TOKEN = "eyJ.token-never-logged.sig"


class FakeBlueapi:
    """A MockTransport handler imitating blueapi 1.17 and a Keycloak token URL."""

    def __init__(self, require_auth: bool = True):
        self.require_auth = require_auth
        self.token_status = 200
        self.plans = ["count", "spec_scan"]
        self.devices = ["det", "sample_stage"]
        self.worker_state: Any = "IDLE"
        self.environment: dict[str, Any] = {
            "environment_id": "0b0e1c56-62c2-4b3a-9d0f-3f0d2c2a4b11",
            "initialized": True,
            "error_message": None,
        }
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        if url == TOKEN_URL:
            return self._token(request)
        path = request.url.path
        if request.method != "GET":
            return httpx.Response(405)
        if path == "/healthz":
            return httpx.Response(200, json={"status": "ok"})
        if self.require_auth and (
            request.headers.get("Authorization") != f"Bearer {TOKEN}"
        ):
            return httpx.Response(401, json={"detail": "Not authenticated"})
        routes = {
            "/api/v1/environment": lambda: self.environment,
            "/api/v1/plans": lambda: {
                "plans": [
                    {"name": n, "description": None, "parameter_schema": {}}
                    for n in self.plans
                ]
            },
            "/api/v1/devices": lambda: {
                "devices": [{"name": n, "protocols": []} for n in self.devices]
            },
            "/api/v1/worker/state": lambda: self.worker_state,
        }
        if path in routes:
            return httpx.Response(200, json=routes[path]())
        return httpx.Response(404)

    def _token(self, request: httpx.Request) -> httpx.Response:
        expected = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
        body = request.content.decode()
        if (
            self.token_status != 200
            or request.method != "POST"
            or request.headers.get("Authorization") != f"Basic {expected}"
            or "grant_type=client_credentials" not in body
        ):
            return httpx.Response(
                self.token_status if self.token_status != 200 else 401,
                json={"error": "unauthorized_client", "error_description": body},
            )
        return httpx.Response(
            200,
            json={"access_token": TOKEN, "expires_in": 300, "token_type": "Bearer"},
        )

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)


@pytest.fixture
def fake() -> FakeBlueapi:
    return FakeBlueapi()


@pytest.fixture
def env(tmp_path) -> dict[str, str]:
    return {
        "BLUEAPI_URL": BLUEAPI + "/",
        "TOKEN_URL": TOKEN_URL,
        "CLIENT_ID": CLIENT_ID,
        "CLIENT_SECRET": CLIENT_SECRET,
        "INSTRUMENT": "p99",
        "JOB_NAME": "nightly-smoke",
        "REPORT_DIR": str(tmp_path / "reports"),
        "EXPECTED_PLANS": "count, spec_scan",
        "EXPECTED_DEVICES": "det",
    }


@pytest.fixture
def config(env) -> Config:
    return Config.from_env(env)
