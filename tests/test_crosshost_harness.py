"""Acceptance reports cannot turn absent infrastructure into an E2E pass."""

import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/check_remote_crosshost.py"


def test_missing_hosts_are_explicitly_not_executed_and_never_accepted(tmp_path):
    subprocess.run(
        [sys.executable, str(SCRIPT), "--not-executed", "--output", str(tmp_path)],
        check=True,
        capture_output=True,
    )
    value = json.loads((tmp_path / "report.json").read_bytes())
    assert value["status"] == "NOT_EXECUTED" and value["full_cross_host_acceptance"] is False
    assert len(value["scenarios"]) == 18 and set(value["scenarios"].values()) == {"NOT_EXECUTED"}


def test_report_refuses_partial_pass_or_missing_os_process_evidence(tmp_path):
    spec = importlib.util.spec_from_file_location("crosshost", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with pytest.raises(ValueError):
        module.report(tmp_path / "report.json", status="PASSED")
    with pytest.raises(ValueError):
        module.report(
            tmp_path / "report.json", status="PASSED", scenarios={k: "PASSED" for k in module.SCENARIOS}
        )
    assert not (tmp_path / "report.json").exists()
    if sys.platform == "linux":
        with pytest.raises(RuntimeError, match="real Windows"):
            module.check({}, tmp_path / "cannot-pass-on-linux")
        assert not (tmp_path / "cannot-pass-on-linux").exists()
