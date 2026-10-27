"""Native queue consent: target/version scope, expiry, replay and pre-send revoke."""

import json
import time

import pytest

from poster.dispatcher.models import DispatcherError
from poster.dispatcher.security import unseal
from poster.dispatcher.service import enqueue_snapshot
from poster.dispatcher.snapshots import Snapshots
from poster.maker.profiles import Profiles
from poster.portability.permissions import SessionPermissions
from poster.portability.transfer import open_session
from test_cloud_runtime import cloud  # noqa: F401
from test_remote_registry import admitted  # noqa: F401


def test_permit_target_scope_cached_transfer_and_revoke(admitted, tmp_path):  # noqa: F811
    store, registry, controller, agent, agent_id, source, *_ = admitted
    registry.approve(agent_id)
    store.db.execute(
        "UPDATE remote_agents SET version='5D.1',last_seen=? WHERE id=?", (time.time(), agent_id)
    )
    manifest, version = Snapshots(store.root).create(source)
    profiles = Profiles(tmp_path / "profiles")
    profile = profiles.create("Sensitive account")
    profiles.open(profile).commit(
        dict(
            cookies=[dict(name="sid", value="SESSION_PERMIT_CANARY", domain="fixture.test", path="/")],
            origins=[],
        )
    )
    permits = SessionPermissions(store)
    arguments = dict(
        agent_id=agent_id,
        project_id=manifest,
        version=version,
        profile_root=profiles.root,
        profile_id=profile,
        origins=["https://fixture.test"],
        ttl=120,
        max_runs=2,
    )
    with pytest.raises(DispatcherError):
        permits.create(**arguments)
    permit = permits.create(**arguments, confirmed=True)
    job = enqueue_snapshot(
        store, manifest, version, source, agent_id=agent_id, session_permission=permit["id"]
    )
    execution = store.claim("controller", 2, 1)
    packet = permits.prepare(permit["id"], agent_id, manifest, version, execution["run_id"], controller)
    assert (
        permits.prepare(permit["id"], agent_id, manifest, version, execution["run_id"], controller) == packet
    )
    assert permits.rows()[0]["remaining"] == 1
    imported = open_session(
        agent,
        controller.public(),
        packet,
        binding=dict(run_id=execution["run_id"], project_id=manifest, version=version),
    )
    assert imported["cookies"][0]["value"] == "SESSION_PERMIT_CANARY"
    assert b"SESSION_PERMIT_CANARY" not in json.dumps(packet).encode()
    with pytest.raises(DispatcherError):
        permits.prepare(permit["id"], agent_id, manifest, "0" * 64, execution["run_id"], controller)
    result = permits.revoke(permit["id"])
    assert result["revoked_before_transfer"] and not result["may_have_been_disclosed"]
    assert store.show(job)["status"] == "CANCELLED"
    assert not permits.before_send(execution["run_id"])
    with pytest.raises(DispatcherError):
        permits.validate(permit["id"], agent_id, manifest, version)


def test_possible_send_marker_and_expired_authorization(admitted, tmp_path):  # noqa: F811
    store, registry, controller, agent, agent_id, source, *_ = admitted
    registry.approve(agent_id)
    store.db.execute(
        "UPDATE remote_agents SET version='5D.1',last_seen=? WHERE id=?", (time.time(), agent_id)
    )
    project_id, version = Snapshots(store.root).create(source)
    profiles = Profiles(tmp_path / "profiles")
    profile = profiles.create("Account")
    profiles.open(profile).commit(
        dict(
            cookies=[],
            origins=[
                dict(
                    origin="https://fixture.test",
                    localStorage=[dict(name="key", value="PRIVATE_PERMIT_STORAGE")],
                )
            ],
        )
    )
    permits = SessionPermissions(store)
    permit = permits.create(
        agent_id, project_id, version, profiles.root, profile, ["https://fixture.test"], confirmed=True
    )
    job = enqueue_snapshot(
        store, project_id, version, source, agent_id=agent_id, session_permission=permit["id"]
    )
    execution = store.claim("controller", 2, 1)
    permits.prepare(permit["id"], agent_id, project_id, version, execution["run_id"], controller)
    assert permits.before_send(execution["run_id"])
    result = permits.revoke(permit["id"])
    assert result["may_have_been_disclosed"] and store.payload(job)["cancel_requested"]
    another = permits.create(
        agent_id, project_id, version, profiles.root, profile, ["https://fixture.test"], confirmed=True
    )
    store.db.execute("UPDATE session_permits SET expires=? WHERE id=?", (time.time() - 1, another["id"]))
    with pytest.raises(DispatcherError):
        permits.validate(another["id"], agent_id, project_id, version)
    row = store.db.execute("SELECT data_cipher FROM session_permits WHERE id=?", (permit["id"],)).fetchone()
    assert unseal(row[0]) == {}
