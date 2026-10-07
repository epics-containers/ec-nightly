"""Generic, parametrised smoke checks against blueapi's REST API.

Routes are those of blueapi 1.17 (``blueapi/service/main.py``): ``/healthz`` is
open; everything under ``/api/v1`` needs a bearer token when blueapi has OIDC
configured. All checks only GET from blueapi, except ``scan``, which runs a
plan (see :func:`check_scan`) and is skipped unless ``SCAN_PLAN`` is set.
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

# the scan check's clock and poll sleep (module level so tests can patch them)
monotonic = time.monotonic
pause = time.sleep


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

    def __init__(
        self,
        config: Config,
        client: httpx.Client,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
    ):
        self.config = config
        self.client = client
        # looked up at construction so tests can replace them
        self.clock = clock or monotonic
        self.sleep = sleep or pause
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

    def request(
        self,
        method: str,
        path: str,
        json: Any = None,
        authenticated: bool = True,
    ) -> httpx.Response:
        """Send a request to blueapi; 401/403 and transport errors raise."""
        headers = {"Accept": "application/json"}
        if authenticated:
            token = self.token()
            if token:
                headers["Authorization"] = f"Bearer {token}"
        url = f"{self.config.blueapi_url}{path}"
        try:
            response = self.client.request(method, url, headers=headers, json=json)
        except httpx.HTTPError as error:
            raise CheckError(
                f"{method} {url} failed: {type(error).__name__}: {error}"
            ) from None
        if response.status_code in (401, 403):
            raise CheckError(
                f"{method} {path} not authorised (HTTP {response.status_code})"
            )
        return response

    def get_json(self, path: str, authenticated: bool = True) -> Any:
        response = self.request("GET", path, authenticated=authenticated)
        if response.status_code != 200:
            raise CheckError(f"GET {path} returned HTTP {response.status_code}")
        return _json(response, f"GET {path}")


def _json(response: httpx.Response, what: str) -> Any:
    try:
        return response.json()
    except ValueError:
        raise CheckError(f"{what} did not return JSON") from None


def _detail(response: httpx.Response, limit: int = 300) -> str:
    """blueapi's error ``detail`` (FastAPI), shortened, for failure messages.

    For a 422 this is the list of pydantic errors on the plan parameters; only
    location and message are kept (not the echoed input).
    """
    try:
        detail = response.json().get("detail")
    except (ValueError, AttributeError):
        return ""
    if isinstance(detail, list):
        parts = []
        for err in detail:
            if isinstance(err, dict):
                loc = ".".join(str(p) for p in err.get("loc", []))
                parts.append(f"{loc}: {err.get('msg')}")
        detail = "; ".join(dict.fromkeys(parts))  # blueapi repeats some errors
    if not isinstance(detail, str) or not detail:
        return ""
    return ": " + _shorten(detail, limit)


def _shorten(text: str, limit: int = 300) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


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


def check_scan(ctx: Context) -> str | None:
    """Run SCAN_PLAN with SCAN_PARAMS on the worker; it must succeed in time.

    POST /api/v1/tasks, PUT /api/v1/worker/task, then poll
    GET /api/v1/tasks/{id} until complete. On timeout, our own task is aborted.
    """
    cfg = ctx.config
    if not cfg.scan_plan:
        return None
    plan = cfg.scan_plan

    # 1. submit: blueapi validates the plan name and parameters here
    response = ctx.request(
        "POST",
        "/api/v1/tasks",
        json={
            "name": plan,
            "params": dict(cfg.scan_params),
            "instrument_session": cfg.scan_instrument_session,
        },
    )
    if response.status_code == 404:
        raise CheckError(f"plan {plan!r} not found (HTTP 404)")
    if response.status_code == 422:
        raise CheckError(f"plan {plan!r} rejected its parameters{_detail(response)}")
    if response.status_code != 201:
        raise CheckError(
            f"submitting plan {plan!r} returned HTTP {response.status_code}"
            f"{_detail(response)}"
        )
    body = _json(response, "POST /api/v1/tasks")
    task_id = body.get("task_id") if isinstance(body, dict) else None
    if not isinstance(task_id, str) or not task_id:
        raise CheckError("POST /api/v1/tasks response has no task_id")
    log.info("scan: submitted %s as task %s", plan, task_id)

    # 2. start it; 409 means the worker is busy with something else
    response = ctx.request("PUT", "/api/v1/worker/task", json={"task_id": task_id})
    if response.status_code != 200:
        _forget(ctx, task_id)
        if response.status_code == 409:
            raise CheckError(f"worker busy, could not start {plan!r} (HTTP 409)")
        raise CheckError(
            f"starting task returned HTTP {response.status_code}{_detail(response)}"
        )

    # 3. wait for it to complete
    started = ctx.clock()
    deadline = started + cfg.scan_timeout
    while True:
        task = ctx.get_json(f"/api/v1/tasks/{task_id}")
        if not isinstance(task, dict):
            raise CheckError("GET /api/v1/tasks/{id} returned unexpected JSON")
        if task.get("is_complete") is True:
            break
        if ctx.clock() >= deadline:
            raise CheckError(
                f"plan {plan!r} did not complete within {cfg.scan_timeout:g}s"
                f"{_abort(ctx, task_id)}"
            )
        ctx.sleep(cfg.scan_poll_interval)
    elapsed = ctx.clock() - started

    # 4. judge the final state
    outcome = task.get("outcome")
    errors = task.get("errors") or []
    if not isinstance(outcome, dict) or outcome.get("outcome") != "success":
        if isinstance(outcome, dict) and outcome.get("outcome") == "error":
            why = f"{outcome.get('type')}: {outcome.get('message')}"
        else:
            why = f"no success outcome ({outcome!r})"
        raise CheckError(f"plan {plan!r} failed: {_shorten(str(why))}")
    if errors:
        raise CheckError(
            f"plan {plan!r} reported {len(errors)} error(s): {_shorten(str(errors[0]))}"
        )
    return f"plan {plan} completed successfully in {elapsed:.1f}s (task {task_id})"


def _forget(ctx: Context, task_id: str) -> None:
    """Best effort: delete a task we submitted but could not start."""
    try:
        ctx.request("DELETE", f"/api/v1/tasks/{task_id}")
    except CheckError as error:
        log.warning("scan: could not delete unstarted task %s: %s", task_id, error)


def _abort(ctx: Context, task_id: str) -> str:
    """Abort the worker's active task, but only if it is the one we started."""
    try:
        active = ctx.get_json("/api/v1/worker/task")
        if not isinstance(active, dict) or active.get("task_id") != task_id:
            return "; it is no longer the active task, not aborted"
        response = ctx.request(
            "PUT",
            "/api/v1/worker/state",
            json={"new_state": "ABORTING", "reason": "ec-nightly scan timeout"},
        )
    except CheckError as error:
        return f"; abort failed: {error}"
    if response.status_code != 202:
        return f"; abort returned HTTP {response.status_code}{_detail(response)}"
    return "; aborted it"


CHECKS: dict[str, Callable[[Context], str | None]] = {
    "token": check_token,
    "health": check_health,
    "environment": check_environment,
    "plans": check_plans,
    "devices": check_devices,
    "worker": check_worker,
    "scan": check_scan,
}

# Why a check that returned None was skipped.
SKIP_REASONS = {
    "token": "skipped: CLIENT_ID/CLIENT_SECRET not set, unauthenticated",
    "scan": "skipped: SCAN_PLAN not set",
}


def run_check(name: str, ctx: Context) -> Result:
    start = time.monotonic()
    try:
        message = CHECKS[name](ctx)
        status = SKIP if message is None else PASS
        if message is None:
            message = SKIP_REASONS.get(name, "skipped")
    except CheckError as error:
        status, message = FAIL, str(error)
    except Exception as error:  # a bug or surprise response must not hide others
        status, message = FAIL, f"unexpected {type(error).__name__}: {error}"
    result = Result(name, status, message, round(time.monotonic() - start, 3))
    level = logging.ERROR if status == FAIL else logging.INFO
    log.log(level, "%-11s %s  %s", name, status.upper(), message)
    return result
