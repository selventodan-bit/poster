"""Acceptance reports cannot turn absent infrastructure into an E2E pass."""

import importlib.util
import json
from pathlib import Path
import subprocess
import sqlite3
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


def test_sqlite_evidence_keeps_real_history_and_excludes_private_columns(tmp_path):
    spec = importlib.util.spec_from_file_location("crosshost", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    source = tmp_path / "original.sqlite"
    with sqlite3.connect(source) as db:
        db.executescript("""
        CREATE TABLE jobs(id,status,attempt,created,inputs_cipher);
        CREATE TABLE executions(run_id,job_id,attempt,status,pid,birth,leased,started,
                                finished,code,effects_possible,report);
        CREATE TABLE remote_runs(run_id,key_cipher);
        INSERT INTO jobs VALUES('job','UNKNOWN',1,1,'PRIVATE_INPUT_SENTINEL');
        INSERT INTO executions VALUES('run','job',1,'UNKNOWN',1234,2,3,4,5,
                                      'REMOTE_LOST',1,'PRIVATE_REPORT_SENTINEL');
        INSERT INTO remote_runs VALUES('run','PRIVATE_KEY_SENTINEL');
        """)
    evidence = module.export_history(source, tmp_path)
    assert evidence["sqlite_history"][0]["pid"] == 1234
    assert evidence["sqlite_history"][0]["status"] == "UNKNOWN"
    assert evidence["sqlite_history"][0]["effects_possible"] == 1
    with sqlite3.connect(tmp_path / "windows-history-safe.sqlite") as db:
        assert db.execute("SELECT run_id,pid,status FROM executions").fetchall() == [("run", 1234, "UNKNOWN")]
        assert {r[1] for r in db.execute("PRAGMA table_info(jobs)")} == {"id", "status", "attempt"}
    assert b"PRIVATE_" not in (tmp_path / "windows-history-safe.sqlite").read_bytes()
    assert b"PRIVATE_KEY_SENTINEL" in (tmp_path / "windows-history-private.sqlite").read_bytes()
