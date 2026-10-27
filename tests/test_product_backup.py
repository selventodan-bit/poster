"""Authenticated snapshots: real WAL data, owner/paths, no plaintext or overwrite."""

import hashlib
import json
import sqlite3
import zipfile

import pytest

from poster.dispatcher.models import DispatcherError
from poster.product import backup, crypto
from poster.product.locking import application_lease
from test_product_crypto import PASSWORD


@pytest.fixture
def roots(tmp_path):
    root = tmp_path / "maker"
    root.mkdir()
    (root / "private").mkdir()
    (root / "private/session.key").write_bytes(b"PRIVATE_SYNTHETIC_KEY")
    (root / "maker-projects").mkdir()
    (root / "maker-projects/project.json").write_text('{"name":"Synthetic"}')
    (root / "browser-cache").mkdir()
    (root / "browser-cache/excluded").write_text("cache")
    return dict(maker=root)


def test_full_encrypted_snapshot_keeps_real_sqlite_wal_and_keys_private(roots, tmp_path):
    db = sqlite3.connect(roots["maker"] / "state.sqlite")
    try:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE data(value TEXT)")
        db.execute("INSERT INTO data VALUES('PRIVATE_SYNTHETIC_SQLITE_VALUE')")
        db.commit()
        output = tmp_path / "backup.pbackup"
        proof = backup.create(roots, output, PASSWORD, confirmed=True)
        assert proof["sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()
        assert b"PRIVATE_SYNTHETIC" not in output.read_bytes()
        manifest = backup.inspect(output, PASSWORD, roots)
        assert not any(
            "browser-cache" in r["path"] or r["path"].endswith(("-wal", "-shm")) for r in manifest["files"]
        )
        with backup.opened(output, PASSWORD, roots, tmp_path) as (archive, manifest):
            item = next(r for r in manifest["files"] if r["path"] == "state.sqlite")
            extracted = tmp_path / "checked.sqlite"
            extracted.write_bytes(archive.read(item["member"]))
            with sqlite3.connect(extracted) as check:
                assert (
                    check.execute("SELECT value FROM data").fetchone()[0] == "PRIVATE_SYNTHETIC_SQLITE_VALUE"
                )
        assert not list(tmp_path.glob(".poster-backup-*")) and not list(tmp_path.glob(".poster-verify-*"))
    finally:
        db.close()


def test_export_requires_consent_offline_and_no_destination_clobber(roots, tmp_path):
    target = tmp_path / "backup.pbackup"
    with pytest.raises(DispatcherError):
        backup.create(roots, target, PASSWORD)
    with application_lease(roots["maker"], "maker"):
        with pytest.raises(DispatcherError) as caught:
            backup.create(roots, target, PASSWORD, confirmed=True)
        assert caught.value.code == "PRODUCT_BUSY"
    backup.create(roots, target, PASSWORD, confirmed=True)
    before = target.read_bytes()
    with pytest.raises(DispatcherError):
        backup.create(roots, target, PASSWORD, confirmed=True)
    assert target.read_bytes() == before
    with pytest.raises(DispatcherError):
        backup.create(roots, roots["maker"] / "recursive.pbackup", PASSWORD, confirmed=True)


@pytest.mark.parametrize(
    "mutation",
    [
        "other_path",
        "other_owner",
        "traversal",
        "duplicate",
        "wrong_hash",
        "unexpected",
        "compressed",
        "prefix_collision",
    ],
)
def test_authenticated_but_invalid_manifest_is_rejected(roots, tmp_path, mutation):
    output = tmp_path / "backup.pbackup"
    backup.create(roots, output, PASSWORD, confirmed=True)
    with backup.opened(output, PASSWORD, roots, tmp_path) as (archive, manifest):
        entries = {i.filename: archive.read(i) for i in archive.infolist()}
    if mutation == "other_path":
        manifest["scopes"][0]["path"] = str(tmp_path / "different")
    elif mutation == "other_owner":
        manifest["binding"] = "unrelated-owner"
    elif mutation == "traversal":
        manifest["files"][0]["path"] = "../escaped"
    elif mutation == "duplicate":
        manifest["files"].append(manifest["files"][0])
    elif mutation == "wrong_hash":
        manifest["files"][0]["sha256"] = "0" * 64
    elif mutation == "unexpected":
        entries["unexpected"] = b"extra"
    elif mutation == "prefix_collision":
        manifest["files"][0]["path"] = "node"
        manifest["files"][1]["path"] = "node/child"
    entries["manifest.json"] = json.dumps(manifest).encode()
    altered = tmp_path / "altered.zip"
    with zipfile.ZipFile(
        altered, "x", compression=zipfile.ZIP_DEFLATED if mutation == "compressed" else zipfile.ZIP_STORED
    ) as archive:
        for name, raw in entries.items():
            archive.writestr(name, raw)
    forged = tmp_path / "forged.pbackup"
    crypto.encrypt(altered, forged, PASSWORD)
    with pytest.raises(DispatcherError):
        backup.inspect(forged, PASSWORD, roots)
    assert not (tmp_path / "escaped").exists()


def test_links_bounds_and_failed_write_preserve_original_data(roots, tmp_path, monkeypatch):
    root = roots["maker"]
    target = tmp_path / "backup.pbackup"
    monkeypatch.setattr(backup, "MAX_FILES", 1)
    with pytest.raises(DispatcherError) as caught:
        backup.create(roots, target, PASSWORD, confirmed=True)
    assert caught.value.code == "BACKUP_LIMIT" and not target.exists()
    monkeypatch.setattr(backup, "MAX_FILES", 20000)
    monkeypatch.setattr(backup, "space", lambda *a: (_ for _ in ()).throw(DispatcherError("BACKUP_LIMIT")))
    with pytest.raises(DispatcherError):
        backup.create(roots, target, PASSWORD, confirmed=True)
    assert (root / "private/session.key").read_bytes() == b"PRIVATE_SYNTHETIC_KEY"
    assert not list(tmp_path.glob(".poster-backup-*"))


def test_actual_hardlink_and_directory_link_cannot_expand_backup_scope(roots, tmp_path):
    import os
    import subprocess

    root = roots["maker"]
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "file.txt").write_text("unselected private data")
    hard = root / "hard.txt"
    hard.hardlink_to(outside / "file.txt")
    with pytest.raises(DispatcherError):
        backup.create(roots, tmp_path / "hard.pbackup", PASSWORD, confirmed=True)
    hard.unlink()
    linked = root / "linked"
    if os.name == "nt":
        result = subprocess.run(["cmd", "/c", "mklink", "/J", str(linked), str(outside)], capture_output=True)
        assert result.returncode == 0
    else:
        linked.symlink_to(outside, target_is_directory=True)
    try:
        with pytest.raises(DispatcherError):
            backup.create(roots, tmp_path / "linked.pbackup", PASSWORD, confirmed=True)
    finally:
        if os.name == "nt":
            linked.rmdir()
        else:
            linked.unlink()
    assert (outside / "file.txt").read_text() == "unselected private data"
