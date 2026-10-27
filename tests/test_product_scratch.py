"""Private staging recovery uses actual PID/birth and ledger ownership."""

import json
from pathlib import Path
import subprocess
import sys

import pytest

from poster.dispatcher.models import DispatcherError
from poster.product import scratch
from poster.product.atomic import durable


def test_live_scratch_is_preserved_and_normal_exit_removes_plaintext():
    with scratch.directory("backup") as (path, identity):
        (path / "plaintext").write_text("SYNTHETIC_PRIVATE")
        assert scratch.owner_alive(scratch.checked(identity))
        assert scratch.cleanup_abandoned() == 0
        assert path.exists()
    assert not path.exists() and not scratch.record_path(identity).exists()


def test_real_process_death_leaves_verified_scratch_for_repeatable_recovery():
    code = """
import os
from poster.product import scratch
with scratch.directory('verify') as (path,identity):
 (path/'plaintext').write_text('SYNTHETIC_PRIVATE')
 print(identity,flush=True)
 os._exit(77)
"""
    process = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=15)
    assert process.returncode == 77
    identity = process.stdout.strip()
    data = scratch.checked(identity)
    assert not scratch.owner_alive(data)
    assert scratch.cleanup_abandoned() == 1
    assert not Path(data["path"]).exists()
    assert scratch.cleanup_abandoned() == 0


def test_changed_ledger_cannot_authorize_deleting_unrelated_data(tmp_path):
    with scratch.directory("verify") as (path, identity):
        record = scratch.record_path(identity)
        original = json.loads(record.read_bytes())
        outside = tmp_path / "unrelated"
        outside.mkdir()
        (outside / "keep").write_text("keep")
        durable(record, dict(original, path=str(outside)))
        try:
            with pytest.raises(DispatcherError):
                scratch.cleanup(identity, own=True)
            assert (outside / "keep").read_text() == "keep" and path.exists()
        finally:
            durable(record, original)


def test_cleanup_phase_tolerates_death_after_directory_removed():
    with scratch.directory("verify") as (path, identity):
        record = scratch.record_path(identity)
        data = scratch.checked(identity)
        durable(record, dict(data, phase="CLEANING"))
        (path / "owner.json").unlink()
        path.rmdir()
        assert scratch.cleanup(identity, own=True)
        assert not record.exists()
