import base64
import json
from typing import Any

import httpx
import pytest

from ec_nightly.config import Config

BLUEAPI = "http://p99-blueapi.p99-beamline.svc.cluster.local:80"
TOKEN_URL = "https://identity.example.org/realms/dls/protocol/openid-connect/token"
CLIENT_ID = "ec-nightly-p99"
CLIENT_SECRET = "fake-secret-never-logged"  # gitleaks:allow (test fixture)
TOKEN = "eyJ.token-never-logged.sig"
TASK_ID = "fe956dbc-a4ba-4b72-8326-10054977f5e0"
SUCCESS = {"outcome": "success", "result": None, "type": "NoneType"}


class FakeBlueapi:
    """A MockTransport handler imitating blueapi 1.17 and a Keycloak token URL."""

    def __init__(self, require_auth: bool = True):
        self.require_auth = require_auth
        self.token_status = 200
        self.plans = ["count", "spec_scan", "num_scan"]
        self.devices = ["det", "sample_stage"]
        self.worker_state: Any = "IDLE"
        self.environment: dict[str, Any] = {
            "environment_id": "0b0e1c56-62c2-4b3a-9d0f-3f0d2c2a4b11",
            "initialized": True,
            "error_message": None,
        }
        self.requests: list[httpx.Request] = []
        # task API (shapes as returned by a real blueapi 1.17.0)
        self.submit_response: httpx.Response | None = None  # override POST /tasks
        self.start_status = 200
        self.polls_to_complete: int | None = 2  # None: never completes
        self.outcome: dict[str, Any] | None = SUCCESS
        self.errors: list[str] = []
        self.abort_status = 202
        self.tasks: dict[str, dict[str, Any]] = {}
        self.active: str | None = None
        self.polls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        if url == TOKEN_URL:
            return self._token(request)
        path = request.url.path
        if path == "/healthz" and request.method == "GET":
            return httpx.Response(200, json={"status": "ok"})
        if self.require_auth and (
            request.headers.get("Authorization") != f"Bearer {TOKEN}"
        ):
            return httpx.Response(401, json={"detail": "Not authenticated"})
        if request.method != "GET" or path.startswith(
            ("/api/v1/tasks", "/api/v1/worker/task")
        ):
            return self._task_api(request, path)
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

    def _task_api(self, request: httpx.Request, path: str) -> httpx.Response:
        method = request.method
        if method == "POST" and path == "/api/v1/tasks":
            body = json.loads(request.content)
            if self.submit_response is not None:
                return self.submit_response
            if body["name"] not in self.plans:
                return httpx.Response(404, json={"detail": "Item not found"})
            if "instrument_session" not in body:
                return httpx.Response(422, json={"detail": [{"loc": ["body"]}]})
            self.tasks[TASK_ID] = {"task": body, "started": False}
            return httpx.Response(201, json={"task_id": TASK_ID})
        if method == "PUT" and path == "/api/v1/worker/task":
            task_id = json.loads(request.content)["task_id"]
            if self.start_status != 200:
                return httpx.Response(self.start_status, json={"detail": "busy"})
            self.tasks[task_id]["started"] = True
            self.active = task_id
            return httpx.Response(200, json={"task_id": task_id})
        if method == "GET" and path == "/api/v1/worker/task":
            return httpx.Response(200, json={"task_id": self.active})
        if method == "PUT" and path == "/api/v1/worker/state":
            return httpx.Response(self.abort_status, json="ABORTING")
        if path.startswith("/api/v1/tasks/"):
            task_id = path.rsplit("/", 1)[1]
            if task_id not in self.tasks:
                return httpx.Response(404, json={"detail": "Item not found"})
            if method == "DELETE":
                del self.tasks[task_id]
                return httpx.Response(200, json={"task_id": task_id})
            if method == "GET":
                self.polls += 1
                done = (
                    self.polls_to_complete is not None
                    and self.polls >= self.polls_to_complete
                )
                if done:
                    self.active = None
                return httpx.Response(
                    200,
                    json={
                        "task_id": task_id,
                        "task": self.tasks[task_id]["task"],
                        "request_id": None,
                        "is_complete": done,
                        "is_pending": not self.tasks[task_id]["started"],
                        "errors": self.errors if done else [],
                        "outcome": self.outcome if done else None,
                    },
                )
        return httpx.Response(405)

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
        "SCAN_PLAN": "num_scan",
        "SCAN_PARAMS": '{"detectors": ["sample_stage"], '
        '"params": [["sample_stage.x", [0, 1]]], "num": 3}',
        "SCAN_INSTRUMENT_SESSION": "cm12345-1",
        "SCAN_TIMEOUT": "60",
        "SCAN_POLL_INTERVAL": "1",
    }


@pytest.fixture
def config(env) -> Config:
    return Config.from_env(env)


class FakeClock:
    """Replaces the scan check's clock: sleeping advances time instantly."""

    def __init__(self):
        self.now = 1000.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture(autouse=True)
def clock(monkeypatch) -> FakeClock:
    clock = FakeClock()
    monkeypatch.setattr("ec_nightly.checks.monotonic", clock.monotonic)
    monkeypatch.setattr("ec_nightly.checks.pause", clock.sleep)
    return clock
