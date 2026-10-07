"""Configuration, read purely from environment variables.

Beamline specifics (URLs, expected plans and devices) are data supplied by the
deployment, e.g. Helm values rendered into the CronJob env, never code.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field

DEFAULT_TOKEN_URL = (
    "https://identity.diamond.ac.uk/realms/dls/protocol/openid-connect/token"
)
DEFAULT_ALLOWED_WORKER_STATES = ("IDLE", "RUNNING", "PAUSED", "PAUSING")


def _list(value: str | None) -> tuple[str, ...]:
    """Parse a comma separated list, ignoring blanks and surrounding space."""
    if not value:
        return ()
    return tuple(item.strip() for item in value.split(",") if item.strip())


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

    @property
    def auth_enabled(self) -> bool:
        return bool(self.client_id or self.client_secret)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Config:
        env = os.environ if env is None else env
        blueapi_url = env.get("BLUEAPI_URL", "").strip().rstrip("/")
        if not blueapi_url:
            raise ValueError("BLUEAPI_URL must be set")
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
        )
