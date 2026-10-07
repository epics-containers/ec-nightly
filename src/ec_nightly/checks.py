"""Generic, parametrised, read-only smoke checks against blueapi's REST API.

Routes are those of blueapi 1.17 (``blueapi/service/main.py``): ``/healthz`` is
open; ``/api/v1/environment``, ``/api/v1/plans``, ``/api/v1/devices`` and
``/api/v1/worker/state`` need a bearer token when blueapi has OIDC configured.
Only GET requests are made against blueapi.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

from .config import Config

log = logging.getLogger(__name__)

PASS, FAIL, SKIP = "pass", "fail", "skip"


class CheckError(Exception):
    """A check found something wrong; the message is safe to log."""


@dataclass
class Result:
    name: str
    status: str
    message: str
    duration: float = 0.0


class Context:
    """Shared state for one run: config, HTTP client and a lazily fetched token.

    The token is held only in memory and is never logged or reported.
    """

    def __init__(self, config: Config, client: httpx.Client):
        self.config = config
        self.client = client
        self._token: str | None = None
        self._token_error: str | None = None
        self.token_expires_in: Any = None

    def token(self) -> str | None:
        """Return a bearer token, or None when auth is not configured."""
        if not self.config.auth_enabled:
            return None
        if self._token is None and self._token_error is None:
            try:
                self._token = self._fetch_token()
            except CheckError as error:
                self._token_error = str(error)
        if self._token is None:
            raise CheckError(f"no bearer token: {self._token_error}")
        return self._token

    def _fetch_token(self) -> str:
        cfg = self.config
        if not (cfg.client_id and cfg.client_secret):
            raise CheckError("CLIENT_ID and CLIENT_SECRET must both be set")
        try:
            # client_secret_basic: credentials in the Authorization header
            response = self.client.post(
                cfg.token_url,
                data={"grant_type": "client_credentials"},
                auth=(cfg.client_id, cfg.client_secret),
            )
        except httpx.HTTPError as error:
            raise CheckError(
                f"token endpoint {cfg.token_url} unreachable: "
                f"{type(error).__name__}: {error}"
            ) from None
        if response.status_code != 200:
            raise CheckError(
                f"token endpoint returned HTTP {response.status_code}"
                f"{_oauth_error(response)}"
            )
        try:
            body = response.json()
            token = body["access_token"]
        except (ValueError, KeyError, TypeError):
            raise CheckError("token endpoint response has no access_token") from None
        if not isinstance(token, str) or not token:
            raise CheckError("token endpoint returned an empty access_token")
        self.token_expires_in = body.get("expires_in")
        return token

    def get_json(self, path: str, authenticated: bool = True) -> Any:
        headers = {"Accept": "application/json"}
        if authenticated:
            token = self.token()
            if token:
                headers["Authorization"] = f"Bearer {token}"
        url = f"{self.config.blueapi_url}{path}"
        try:
            response = self.client.get(url, headers=headers)
        except httpx.HTTPError as error:
            raise CheckError(
                f"GET {url} failed: {type(error).__name__}: {error}"
            ) from None
        if response.status_code in (401, 403):
            raise CheckError(f"GET {path} not authorised (HTTP {response.status_code})")
        if response.status_code != 200:
            raise CheckError(f"GET {path} returned HTTP {response.status_code}")
        try:
            return response.json()
        except ValueError:
            raise CheckError(f"GET {path} did not return JSON") from None


def _oauth_error(response: httpx.Response) -> str:
    """Only the RFC 6749 ``error`` code is echoed, never the raw body."""
    try:
        error = response.json().get("error")
    except (ValueError, AttributeError):
        return ""
    return f": {error}" if isinstance(error, str) else ""


def _names(items: Any, key: str, path: str) -> list[str]:
    if not isinstance(items, dict) or not isinstance(items.get(key), list):
        raise CheckError(f"GET {path} response has no '{key}' list")
    return [i["name"] for i in items[key] if isinstance(i, dict) and "name" in i]


def _expect(kind: str, found: list[str], expected: tuple[str, ...]) -> str:
    if not found:
        raise CheckError(f"no {kind} available")
    missing = [name for name in expected if name not in found]
    if missing:
        raise CheckError(
            f"{len(missing)} expected {kind} missing: {', '.join(missing)}"
        )
    extra = f", all {len(expected)} expected present" if expected else ""
    return f"{len(found)} {kind}{extra}"


# Each check returns a short success message or raises CheckError.
# A check returning None is reported as skipped.


def check_token(ctx: Context) -> str | None:
    """An OAuth2 client_credentials token can be obtained from TOKEN_URL."""
    if not ctx.config.auth_enabled:
        return None
    ctx.token()
    return f"token obtained (expires_in={ctx.token_expires_in})"


def check_health(ctx: Context) -> str:
    """blueapi is reachable: GET /healthz (unauthenticated) reports ok."""
    body = ctx.get_json("/healthz", authenticated=False)
    status = body.get("status") if isinstance(body, dict) else None
    if status != "ok":
        raise CheckError(f"/healthz status is {status!r}, expected 'ok'")
    return "healthz ok"


def check_environment(ctx: Context) -> str:
    """Authenticated GET /api/v1/environment succeeds and it is initialised."""
    path = "/api/v1/environment"
    body = ctx.get_json(path)
    if not isinstance(body, dict):
        raise CheckError(f"GET {path} returned unexpected JSON")
    error = body.get("error_message") or body.get("errorMessage")
    if error:
        raise CheckError(f"environment error: {error}")
    if body.get("initialized") is not True:
        raise CheckError("environment not initialised")
    return "authenticated; environment initialised"


def check_plans(ctx: Context) -> str:
    """Plans list is non-empty and contains every EXPECTED_PLANS entry."""
    path = "/api/v1/plans"
    names = _names(ctx.get_json(path), "plans", path)
    return _expect("plans", names, ctx.config.expected_plans)


def check_devices(ctx: Context) -> str:
    """Devices list is non-empty and contains every EXPECTED_DEVICES entry."""
    path = "/api/v1/devices"
    names = _names(ctx.get_json(path), "devices", path)
    return _expect("devices", names, ctx.config.expected_devices)


def check_worker(ctx: Context) -> str:
    """Worker state is one of ALLOWED_WORKER_STATES (not PANICKED/UNKNOWN)."""
    state = ctx.get_json("/api/v1/worker/state")
    allowed = ctx.config.allowed_worker_states
    if not isinstance(state, str) or state.upper() not in allowed:
        raise CheckError(f"worker state {state!r} not in allowed {', '.join(allowed)}")
    return f"worker state {state}"


CHECKS: dict[str, Callable[[Context], str | None]] = {
    "token": check_token,
    "health": check_health,
    "environment": check_environment,
    "plans": check_plans,
    "devices": check_devices,
    "worker": check_worker,
}


def run_check(name: str, ctx: Context) -> Result:
    start = time.monotonic()
    try:
        message = CHECKS[name](ctx)
        status = SKIP if message is None else PASS
        if message is None:
            message = "skipped: CLIENT_ID/CLIENT_SECRET not set, unauthenticated"
    except CheckError as error:
        status, message = FAIL, str(error)
    except Exception as error:  # a bug or surprise response must not hide others
        status, message = FAIL, f"unexpected {type(error).__name__}: {error}"
    result = Result(name, status, message, round(time.monotonic() - start, 3))
    level = logging.ERROR if status == FAIL else logging.INFO
    log.log(level, "%-11s %s  %s", name, status.upper(), message)
    return result
