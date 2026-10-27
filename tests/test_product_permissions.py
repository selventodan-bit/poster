"""Restoring an offered native remote run preserves identity but cannot grant again."""

import time
import pytest

from poster.dispatcher.models import DispatcherError
from poster.dispatcher.store import Store
from poster.dispatcher.workspace import queue_key
from poster.dispatcher.snapshots import Snapshots
from poster.dispatcher.service import enqueue_snapshot
from poster.maker.example_projects import chain
from poster.maker.storage import Vault, save_graph
from poster.maker.profiles import Profiles
from poster.portability.permissions import SessionPermissions
from poster.remote.protocol import Identity
from poster.remote.registry import Registry, validate_target
from poster.remote.controller import Controller
from poster.product import backup, restore
from test_product_crypto import PASSWORD


def test_restored_offers_transfers_and_grants_are_locally_revoked_but_identity_is_kept(tmp_path):
    roots = {"maker": tmp_path / "maker", "dispatcher": tmp_path / "dispatcher"}
    roots["maker"].mkdir()
    source = roots["maker"] / "remote.postergraph.json"
    save_graph(
        source, chain("Remote recovery", [("Files.WriteText", dict(path="result.txt", text="ok"))]), Vault()
    )
    queue_key(roots["dispatcher"])
    store = Store(roots["dispatcher"] / "dispatcher.sqlite")
    agent = Identity()
    registry = Registry(store)
    controller = registry.identity()
    agent_id = registry.register(
        dict(peer=agent.public(), name="Linux fixture", os="linux", version="5D.1", protocol=1),
        dict(url="https://relay.example.org", token="x" * 40, channel="a" * 32),
    )
    registry.approve(agent_id)
    store.db.execute("UPDATE remote_agents SET last_seen=?", (time.time(),))
    project_id, version = Snapshots(store.root).create(source)
    profiles = Profiles(roots["maker"] / "browser-profiles")
    profile = profiles.create("Restore fixture")
    profiles.open(profile).commit(
        dict(
            cookies=[dict(name="sid", value="SYNTHETIC_RECOVERY_COOKIE", domain="fixture.test", path="/")],
            origins=[],
        )
    )
    permits = SessionPermissions(store)
    permit = permits.create(
        agent_id, project_id, version, profiles.root, profile, ["https://fixture.test"], confirmed=True
    )
    job = enqueue_snapshot(
        store, project_id, version, source, agent_id=agent_id, session_permission=permit["id"]
    )
    execution = store.claim("fixture-controller", 2, 1)
    permits.prepare(permit["id"], agent_id, project_id, version, execution["run_id"], controller)
    network = Controller(store.path)
    network.store, network.identity = store, controller
    network.offer(store.is_remote(execution["run_id"]))
    assert store.db.execute("SELECT count(*) FROM remote_outbox").fetchone()[0] > 0
    assert store.db.execute("SELECT count(*) FROM session_transfers").fetchone()[0] == 1
    store.close()
    path = tmp_path / "permissions.pbackup"
    proof = backup.create(roots, path, PASSWORD, confirmed=True)
    result = restore.restore(path, PASSWORD, roots, expected_sha256=proof["sha256"], confirmed=True)
    assert result["remote_revocation"] == "UNCONFIRMED"
    store = Store(roots["dispatcher"] / "dispatcher.sqlite")
    try:
        assert Registry(store).identity().public()["id"] == controller.public()["id"]
        assert store.show(job)["status"] == "REVIEW_REQUIRED"
        assert store.claim("restart", 2, 1) is None
        assert store.db.execute("SELECT count(*) FROM remote_outbox").fetchone()[0] == 0
        assert store.db.execute("SELECT count(*) FROM session_transfers").fetchone()[0] == 0
        row = SessionPermissions(store).rows()[0]
        assert row["revoked"] and row["remaining"] == 0
        with pytest.raises(DispatcherError):
            SessionPermissions(store).validate(permit["id"], agent_id, project_id, version)
        with pytest.raises(DispatcherError):
            validate_target(store, agent_id)
        assert (
            Profiles(profiles.root).open(profile).data["state"]["cookies"][0]["value"]
            == "SYNTHETIC_RECOVERY_COOKIE"
        )
    finally:
        store.close()
