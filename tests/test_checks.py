import dataclasses
import json
import logging
import xml.etree.ElementTree as ET
from pathlib import Path

import httpx
import pytest

from conftest import BLUEAPI, CLIENT_SECRET, TOKEN, TOKEN_URL, FakeBlueapi
from ec_nightly.__main__ import run_checks
from ec_nightly.checks import CHECKS
from ec_nightly.config import DEFAULT_TOKEN_URL, Config


def report(config: Config) -> dict:
    (path,) = Path(config.report_dir).glob("*/*/report.json")
    return json.loads(path.read_text())


def statuses(config: Config) -> dict[str, str]:
    return {r["name"]: r["status"] for r in report(config)["results"]}


def test_config_from_env(config):
    assert config.blueapi_url == BLUEAPI
    assert config.expected_plans == ("count", "spec_scan")
    assert config.expected_devices == ("det",)
    assert config.allowed_worker_states == ("IDLE", "RUNNING", "PAUSED", "PAUSING")
    assert CLIENT_SECRET not in repr(config)


def test_config_defaults():
    config = Config.from_env({"BLUEAPI_URL": BLUEAPI})
    assert config.token_url == DEFAULT_TOKEN_URL
    assert config.job_name == "local"
    assert not config.auth_enabled
    assert config.expected_plans == ()


def test_config_requires_blueapi_url():
    with pytest.raises(ValueError, match="BLUEAPI_URL"):
        Config.from_env({})


def test_all_pass(config, fake, caplog, capsys):
    caplog.set_level(logging.DEBUG)
    assert run_checks([], config, fake.transport) == 0
    assert set(statuses(config).values()) == {"pass"}
    assert list(statuses(config)) == list(CHECKS)

    # one token request, sent client_secret_basic, and bearer on secure routes
    token_requests = [r for r in fake.requests if str(r.url) == TOKEN_URL]
    assert len(token_requests) == 1
    for request in fake.requests:
        if request.url.path.startswith("/api/v1/"):
            assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        if request.url.path == "/healthz":
            assert "Authorization" not in request.headers
    # only the scan check writes to blueapi: submit and start one task
    writes = [
        (r.method, r.url.path)
        for r in fake.requests
        if r.method != "GET" and str(r.url) != TOKEN_URL
    ]
    assert writes == [("POST", "/api/v1/tasks"), ("PUT", "/api/v1/worker/task")]

    # never leak secrets anywhere
    out = caplog.text + capsys.readouterr().out
    out += "".join(p.read_text() for p in Path(config.report_dir).rglob("*.*"))
    assert CLIENT_SECRET not in out
    assert TOKEN not in out


def test_junit_report(config, fake):
    fake.devices = ["sample_stage"]
    assert run_checks([], config, fake.transport) == 1
    (path,) = Path(config.report_dir).glob("*/nightly-smoke/report.xml")
    suite = ET.parse(path).getroot()
    assert suite.get("tests") == str(len(CHECKS))
    assert suite.get("failures") == "1"
    failure = suite.find("testcase[@name='devices']/failure")
    assert failure is not None
    assert "det" in failure.get("message", "")
    assert report(config)["passed"] is False


def test_missing_plan_fails(config, fake):
    fake.plans = ["count"]
    assert run_checks(["plans"], config, fake.transport) == 1
    (result,) = report(config)["results"]
    assert result["status"] == "fail"
    assert "spec_scan" in result["message"]


def test_empty_devices_fail_even_with_no_expectation(config, fake):
    config = dataclasses.replace(config, expected_devices=())
    fake.devices = []
    assert run_checks(["devices"], config, fake.transport) == 1


@pytest.mark.parametrize("state", ["PANICKED", "UNKNOWN", 42])
def test_bad_worker_state_fails(config, fake, state):
    fake.worker_state = state
    assert run_checks(["worker"], config, fake.transport) == 1


def test_allowed_worker_states_configurable(env, fake):
    env["ALLOWED_WORKER_STATES"] = "idle"
    config = Config.from_env(env)
    fake.worker_state = "RUNNING"
    assert run_checks(["worker"], config, fake.transport) == 1
    fake.worker_state = "IDLE"
    assert run_checks(["worker"], config, fake.transport) == 0


def test_environment_error_fails(config, fake):
    fake.environment = {**fake.environment, "initialized": False}
    assert run_checks(["environment"], config, fake.transport) == 1
    fake.environment = {**fake.environment, "error_message": "boom"}
    assert run_checks(["environment"], config, fake.transport) == 1


def test_bad_credentials(config, fake, caplog, capsys):
    caplog.set_level(logging.DEBUG)
    config = dataclasses.replace(config, client_secret="wrong-" + CLIENT_SECRET)
    assert run_checks([], config, fake.transport) == 1
    result = statuses(config)
    assert result["token"] == "fail"
    assert result["health"] == "pass"
    for name in ("environment", "plans", "devices", "worker", "scan"):
        assert result[name] == "fail"
    # the token endpoint is tried once, its error_description is not echoed
    assert sum(r.method == "POST" for r in fake.requests) == 1
    out = caplog.text + capsys.readouterr().out
    out += "".join(p.read_text() for p in Path(config.report_dir).rglob("*.*"))
    assert "unauthorized_client" in out
    assert CLIENT_SECRET not in out
    assert "grant_type" not in out


def test_unauthenticated_blueapi(env, fake):
    del env["CLIENT_ID"], env["CLIENT_SECRET"]
    config = Config.from_env(env)
    fake.require_auth = False
    assert run_checks([], config, fake.transport) == 0
    assert statuses(config)["token"] == "skip"
    assert all("Authorization" not in r.headers for r in fake.requests)


def test_auth_required_but_not_configured(env, fake):
    del env["CLIENT_ID"], env["CLIENT_SECRET"]
    config = Config.from_env(env)
    assert run_checks(["plans"], config, fake.transport) == 1
    assert "not authorised (HTTP 401)" in report(config)["results"][0]["message"]


def test_half_configured_credentials(env, fake):
    del env["CLIENT_SECRET"]
    assert run_checks(["token"], Config.from_env(env), fake.transport) == 1


def test_unreachable(config):
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    assert run_checks([], config, httpx.MockTransport(refuse)) == 1
    assert set(statuses(config).values()) == {"fail"}


def test_unexpected_json_does_not_crash(config):
    def weird(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"access_token": TOKEN})
        return httpx.Response(200, json=["not", "a", "dict"])

    assert run_checks([], config, httpx.MockTransport(weird)) == 1
    assert statuses(config)["token"] == "pass"


def test_no_report_dir(env, fake, tmp_path):
    env["REPORT_DIR"] = ""
    assert run_checks([], Config.from_env(env), fake.transport) == 0
    assert not (tmp_path / "reports").exists()


def test_unwritable_report_dir(env, fake, tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("")
    env["REPORT_DIR"] = str(blocker)
    assert run_checks([], Config.from_env(env), fake.transport) == 1


def test_read_only_without_scan_plan(env, fake):
    # guard: with SCAN_PLAN unset the checks only ever GET from blueapi
    del env["SCAN_PLAN"]
    config = Config.from_env(env)
    assert run_checks([], config, fake.transport) == 0
    assert statuses(config)["scan"] == "skip"
    assert {r.method for r in fake.requests if str(r.url) != TOKEN_URL} == {"GET"}


def test_fake_rejects_unknown_writes():
    fake = FakeBlueapi(require_auth=False)
    assert fake(httpx.Request("POST", BLUEAPI + "/api/v1/plans")).status_code == 405
