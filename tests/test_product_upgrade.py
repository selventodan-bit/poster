"""Upgrade real accepted Stage 5D data, produced by its unmodified source."""

import io
import json
import os
import subprocess
import sys
import tarfile

import pytest

from poster.product import backup, restore
from poster.dispatcher.store import Store
from poster.dispatcher.workspace import queue_key
from poster.remote.registry import Registry
from poster.cloud.auth import SecretStore
from test_product_crypto import PASSWORD

BASELINE = "f733210a89a7a30d3d87669f85942bb403e0f9df"


@pytest.mark.parametrize(
    "baseline",
    [
        "3aa036d10882cb505706e0751389e73a221db0e2",
        "f4a9efa32973e47197a20148af6bb52ca3db5e9e",
        "f411f806d5d0fa498b556bd4e4d2c6a955bcd992",
        "78971e96952a24202e7b1cbb50d0dd0579b8a0c3",
        BASELINE,
    ],
    ids=["4B2", "5A", "5B", "5C", "5D"],
)
def test_accepted_producer_keys_projects_history_and_encrypted_cloud_survive(tmp_path, baseline):
    package = subprocess.check_output(["git", "archive", baseline, "poster"], timeout=30)
    source = tmp_path / "accepted-source"
    source.mkdir()
    with tarfile.open(fileobj=io.BytesIO(package)) as archive:
        archive.extractall(source, filter="data")
    roots = {name: tmp_path / name for name in ["maker", "dispatcher", "cloud"]}
    for root in roots.values():
        root.mkdir()
    code = """
import json,sys
from pathlib import Path
from poster.dispatcher.store import Store
from poster.dispatcher.workspace import queue_key
from poster.maker.example_projects import chain
from poster.maker.storage import Vault,save_graph
p=json.load(sys.stdin);roots={k:Path(v) for k,v in p.items()}
save_graph(roots['maker']/'upgrade.postergraph.json',chain('Accepted 5D',[('Files.WriteText',dict(path='result.txt',text='kept'))]),Vault())
key=queue_key(roots['dispatcher']);store=Store(roots['dispatcher']/'dispatcher.sqlite')
identity=None
if Path('poster/remote/registry.py').is_file():
 from poster.remote.registry import Registry
 identity=Registry(store).identity().public()['id']
from poster.dispatcher.service import enqueue
job=enqueue(store,roots['maker']/'upgrade.postergraph.json',key='upgrade-pending')
store.close()
has_cloud=Path('poster/cloud/auth.py').is_file()
if has_cloud:
 from poster.cloud.auth import SecretStore
 secret=SecretStore(roots['cloud']);secret.put('synthetic','STAGE5D_CIPHER')
(roots['maker']/'result.txt').write_text('accepted-result')
print(json.dumps(dict(identity=identity,job=job,cloud=has_cloud)))
"""
    env = dict(os.environ, PYTHONPATH=str(source))
    old = subprocess.run(
        [sys.executable, "-c", code],
        cwd=source,
        env=env,
        input=json.dumps({k: str(v) for k, v in roots.items()}),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert old.returncode == 0, old.stderr
    prior = json.loads(old.stdout)
    identity = prior["identity"]
    store = Store(roots["dispatcher"] / "dispatcher.sqlite")
    try:
        assert store.show(prior["job"])["status"] == "PENDING"
    finally:
        store.close()
    old_key = (roots["dispatcher"] / "queue.key").read_bytes()
    output = tmp_path / "upgrade.pbackup"
    proof = backup.create(roots, output, PASSWORD, confirmed=True)
    (roots["maker"] / "result.txt").write_text("new-version")
    restore.restore(output, PASSWORD, roots, expected_sha256=proof["sha256"], confirmed=True)
    assert (roots["maker"] / "result.txt").read_text() == "accepted-result"
    assert (roots["maker"] / "upgrade.postergraph.json").is_file()
    assert queue_key(roots["dispatcher"]) == old_key.strip()
    store = Store(roots["dispatcher"] / "dispatcher.sqlite")
    try:
        if identity is not None:
            assert Registry(store).identity().public()["id"] == identity
        assert store.show(prior["job"])["status"] == "CANCELLED"
        assert store.db.execute("PRAGMA user_version").fetchone()[0] == 7
    finally:
        store.close()
    if prior["cloud"]:
        secret = SecretStore(roots["cloud"])
        try:
            assert secret.get("synthetic") == "STAGE5D_CIPHER"
        finally:
            secret.close()
