import subprocess
import sys

import pytest

from ec_nightly import __version__
from ec_nightly.__main__ import main


def test_cli_version():
    cmd = [sys.executable, "-m", "ec_nightly", "--version"]
    assert subprocess.check_output(cmd).decode().strip() == __version__


def test_list(capsys):
    main(["list"])
    out = capsys.readouterr().out
    for name in (
        "token",
        "health",
        "environment",
        "plans",
        "devices",
        "worker",
        "scan",
    ):
        assert name in out


def test_unknown_check():
    with pytest.raises(SystemExit) as exc:
        main(["run", "nonsense"])
    assert exc.value.code == 2


def test_missing_blueapi_url(monkeypatch):
    monkeypatch.delenv("BLUEAPI_URL", raising=False)
    with pytest.raises(SystemExit) as exc:
        main(["run"])
    assert exc.value.code == 2


def test_no_command(capsys):
    main([])
    assert "usage" in capsys.readouterr().out
