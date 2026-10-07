"""Configuration, read purely from environment variables.

Beamline specifics (URLs, expected plans and devices, the scan to run) are
data supplied by the deployment, e.g. Helm values rendered into the CronJob
env, never code.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

DEFAULT_TOKEN_URL = (
    "https://identity.diamond.ac.uk/realms/dls/protocol/openid-connect/token"
)
DEFAULT_ALLOWED_WORKER_STATES = ("IDLE", "RUNNING", "PAUSED", "PAUSING")
DEFAULT_SCAN_TIMEOUT = 300.0
DEFAULT_SCAN_POLL_INTERVAL = 2.0


def _list(value: str | None) -> tuple[str, ...]:
    """Parse a comma separated list, ignoring blanks and surrounding space."""
    if not value:
        return ()
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _scan_params(value: str | None) -> dict[str, Any]:
    """Parse SCAN_PARAMS: a JSON object of plan parameters (default empty)."""
    if not value or not value.strip():
        return {}
    try:
        params = json.loads(value)
    except ValueError as error:
        raise ValueError(f"SCAN_PARAMS is not valid JSON: {error}") from None
    if not isinstance(params, dict):
        raise ValueError('SCAN_PARAMS must be a JSON object, e.g. {"num": 1}')
    return params


def _seconds(env: Mapping[str, str], name: str, default: float) -> float:
    raw = env.get(name, "").strip()
    try:
        value = float(raw) if raw else default
    except ValueError:
        raise ValueError(f"{name} must be a number of seconds, got {raw!r}") from None
    if value <= 0:
        raise ValueError(f"{name} must be positive, got {raw!r}")
    return value


@dataclass(frozen=True)
class Config:
    blueapi_url: str
    token_url: str = DEFAULT_TOKEN_URL
    client_id: str = ""
    client_secret: str = field(default="", repr=False)
    instrument: str = ""
    job_name: str = "local"
    report_dir: str = ""
    expected_plans: tuple[str, ...] = ()
    expected_devices: tuple[str, ...] = ()
    allowed_worker_states: tuple[str, ...] = DEFAULT_ALLOWED_WORKER_STATES
    timeout: float = 30.0
    scan_plan: str = ""
    scan_params: Mapping[str, Any] = field(default_factory=dict)
    scan_instrument_session: str = ""
    scan_timeout: float = DEFAULT_SCAN_TIMEOUT
    scan_poll_interval: float = DEFAULT_SCAN_POLL_INTERVAL

    @property
    def auth_enabled(self) -> bool:
        return bool(self.client_id or self.client_secret)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Config:
        env = os.environ if env is None else env
        blueapi_url = env.get("BLUEAPI_URL", "").strip().rstrip("/")
        if not blueapi_url:
            raise ValueError("BLUEAPI_URL must be set")
        scan_plan = env.get("SCAN_PLAN", "").strip()
        scan_session = env.get("SCAN_INSTRUMENT_SESSION", "").strip()
        if scan_plan and not scan_session:
            raise ValueError(
                "SCAN_INSTRUMENT_SESSION must be set when SCAN_PLAN is set "
                "(blueapi requires an instrument session for every task)"
            )
        return cls(
            blueapi_url=blueapi_url,
            token_url=env.get("TOKEN_URL", "").strip() or DEFAULT_TOKEN_URL,
            client_id=env.get("CLIENT_ID", "").strip(),
            client_secret=env.get("CLIENT_SECRET", "").strip(),
            instrument=env.get("INSTRUMENT", "").strip(),
            job_name=env.get("JOB_NAME", "").strip() or "local",
            report_dir=env.get("REPORT_DIR", "").strip(),
            expected_plans=_list(env.get("EXPECTED_PLANS")),
            expected_devices=_list(env.get("EXPECTED_DEVICES")),
            allowed_worker_states=tuple(
                s.upper() for s in _list(env.get("ALLOWED_WORKER_STATES"))
            )
            or DEFAULT_ALLOWED_WORKER_STATES,
            timeout=float(env.get("HTTP_TIMEOUT", "") or 30.0),
            scan_plan=scan_plan,
            scan_params=_scan_params(env.get("SCAN_PARAMS")),
            scan_instrument_session=scan_session,
            scan_timeout=_seconds(env, "SCAN_TIMEOUT", DEFAULT_SCAN_TIMEOUT),
            scan_poll_interval=_seconds(
                env, "SCAN_POLL_INTERVAL", DEFAULT_SCAN_POLL_INTERVAL
            ),
        )
