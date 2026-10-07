import dataclasses
import json
import logging
from pathlib import Path

import httpx
import pytest

from conftest import CLIENT_SECRET, TASK_ID, TOKEN, FakeBlueapi, FakeClock
from ec_nightly.__main__ import run_checks
from ec_nightly.config import Config


def result(config: Config) -> dict:
    (path,) = Path(config.report_dir).glob("*/*/report.json")
    (only,) = json.loads(path.read_text())["results"]
    return only


def writes(fake: FakeBlueapi) -> list[tuple[str, str]]:
    return [
        (r.method, r.url.path)
        for r in fake.requests
        if r.method != "GET" and r.url.path.startswith("/api/v1/")
    ]


def test_scan_config(config):
    assert config.scan_plan == "num_scan"
    assert config.scan_params == {
        "detectors": ["sample_stage"],
        "params": [["sample_stage.x", [0, 1]]],
        "num": 3,
    }
    assert config.scan_instrument_session == "cm12345-1"
    assert config.scan_timeout == 60
    assert config.scan_poll_interval == 1


def test_scan_defaults():
    config = Config.from_env({"BLUEAPI_URL": "http://x"})
    assert config.scan_plan == ""
    assert config.scan_params == {}
    assert config.scan_timeout == 300
    assert config.scan_poll_interval == 2


@pytest.mark.parametrize(
    "key, value, match",
    [
        ("SCAN_PARAMS", "{not json", "not valid JSON"),
        ("SCAN_PARAMS", '["a list"]', "JSON object"),
        ("SCAN_TIMEOUT", "soon", "number of seconds"),
        ("SCAN_TIMEOUT", "0", "positive"),
        ("SCAN_POLL_INTERVAL", "-1", "positive"),
        ("SCAN_INSTRUMENT_SESSION", "", "SCAN_INSTRUMENT_SESSION"),
    ],
)
def test_bad_scan_config(env, key, value, match):
    env[key] = value
    with pytest.raises(ValueError, match=match):
        Config.from_env(env)


def test_scan_success(config, fake, clock: FakeClock, caplog, capsys):
    caplog.set_level(logging.DEBUG)
    assert run_checks(["scan"], config, fake.transport) == 0
    r = result(config)
    assert r["status"] == "pass"
    assert "num_scan completed successfully" in r["message"]
    assert TASK_ID in r["message"]

    # submit then start, with the task as blueapi 1.17 expects it
    assert writes(fake) == [("POST", "/api/v1/tasks"), ("PUT", "/api/v1/worker/task")]
    submit, start = (
        r
        for r in fake.requests
        if r.method in ("POST", "PUT") and r.url.path.startswith("/api/v1/")
    )
    assert json.loads(submit.content) == {
        "name": "num_scan",
        "params": config.scan_params,
        "instrument_session": "cm12345-1",
    }
    assert json.loads(start.content) == {"task_id": TASK_ID}
    for request in fake.requests:
        if request.url.path.startswith("/api/v1/"):
            assert request.headers["Authorization"] == f"Bearer {TOKEN}"
    # polled at SCAN_POLL_INTERVAL until complete
    assert clock.sleeps == [1.0]

    out = caplog.text + capsys.readouterr().out
    out += "".join(p.read_text() for p in Path(config.report_dir).rglob("*.*"))
    assert CLIENT_SECRET not in out
    assert TOKEN not in out


def test_scan_skipped_without_plan(env, fake):
    del env["SCAN_PLAN"]
    config = Config.from_env(env)
    assert run_checks(["scan"], config, fake.transport) == 0
    assert result(config)["status"] == "skip"
    assert "SCAN_PLAN" in result(config)["message"]
    assert writes(fake) == []


def test_scan_plan_error(config, fake):
    # as blueapi 1.17 reports a plan that raised (seen against a real server)
    message = "FileNotFoundError(\"Path /tmp/ doesn't exist or not writable!\")"
    fake.outcome = {"outcome": "error", "type": "FailedStatus", "message": message}
    fake.errors = [message]
    assert run_checks(["scan"], config, fake.transport) == 1
    r = result(config)
    assert r["status"] == "fail"
    assert "failed: FailedStatus" in r["message"]
    assert "not writable" in r["message"]


def test_scan_errors_with_success_outcome_fail(config, fake):
    fake.errors = ["something went wrong"]
    assert run_checks(["scan"], config, fake.transport) == 1
    assert "1 error(s): something went wrong" in result(config)["message"]


def test_scan_complete_without_outcome_fails(config, fake):
    fake.outcome = None
    assert run_checks(["scan"], config, fake.transport) == 1
    assert "no success outcome" in result(config)["message"]


def test_scan_timeout_aborts_own_task(config, fake, clock):
    fake.polls_to_complete = None
    assert run_checks(["scan"], config, fake.transport) == 1
    r = result(config)
    assert "did not complete within 60s; aborted it" in r["message"]
    assert writes(fake)[-1] == ("PUT", "/api/v1/worker/state")
    abort = fake.requests[-1]
    assert json.loads(abort.content)["new_state"] == "ABORTING"
    assert sum(clock.sleeps) == 60


def test_scan_timeout_does_not_abort_other_task(config, fake):
    fake.polls_to_complete = None

    def someone_else(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/worker/task" and request.method == "GET":
            return httpx.Response(200, json={"task_id": "not-ours"})
        return fake(request)

    assert run_checks(["scan"], config, httpx.MockTransport(someone_else)) == 1
    assert "not aborted" in result(config)["message"]
    assert ("PUT", "/api/v1/worker/state") not in writes(fake)


def test_scan_timeout_abort_refused(config, fake):
    fake.polls_to_complete = None
    fake.abort_status = 400
    assert run_checks(["scan"], config, fake.transport) == 1
    assert "abort returned HTTP 400" in result(config)["message"]


def test_scan_forbidden(config):
    # e.g. the service account's token is valid but lacks the role to run plans
    fake = FakeBlueapi()
    forbidden = httpx.Response(403, json={"detail": "Forbidden"})
    fake.submit_response = forbidden
    assert run_checks(["scan"], config, fake.transport) == 1
    assert "POST /api/v1/tasks not authorised (HTTP 403)" in result(config)["message"]
    assert writes(fake) == [("POST", "/api/v1/tasks")]


def test_scan_unknown_plan(config, fake):
    config = dataclasses.replace(config, scan_plan="no_such_plan")
    assert run_checks(["scan"], config, fake.transport) == 1
    assert "plan 'no_such_plan' not found (HTTP 404)" in result(config)["message"]


def test_scan_bad_params(config, fake):
    # blueapi 1.17's 422 body for an unknown device, as returned by a real server
    err = {
        "loc": ["body", "params", "detectors", 0, "function-after[valid(), str]"],
        "msg": "Value error, Device nodev cannot be found",
        "type": "value_error",
        "input": "nodev",
    }
    fake.submit_response = httpx.Response(422, json={"detail": [err, err]})
    assert run_checks(["scan"], config, fake.transport) == 1
    message = result(config)["message"]
    assert "rejected its parameters" in message
    assert message.count("Device nodev cannot be found") == 1  # de-duplicated


def test_scan_worker_busy_deletes_task(config, fake):
    fake.start_status = 409
    assert run_checks(["scan"], config, fake.transport) == 1
    assert "worker busy" in result(config)["message"]
    assert writes(fake)[-1] == ("DELETE", f"/api/v1/tasks/{TASK_ID}")
    assert fake.tasks == {}


def test_scan_unexpected_submit_response(config, fake):
    fake.submit_response = httpx.Response(201, json={"no": "id"})
    assert run_checks(["scan"], config, fake.transport) == 1
    assert "no task_id" in result(config)["message"]
    fake.submit_response = httpx.Response(500, text="oops")
    assert run_checks(["scan"], config, fake.transport) == 1
