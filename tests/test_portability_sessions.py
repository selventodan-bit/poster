"""Existing Profiles plus real browsers and target-bound encrypted session transfer."""

import json

from filelock import FileLock
import pytest

from poster.maker.profiles import Profiles
from poster.maker.model import GraphError
from poster.maker.runtime import BrowserSession
from poster.portability.sessions import normalize, compatibility, select_origins
from poster.portability.transfer import seal_session, open_session, export_file
from poster.remote.protocol import Identity
from poster.maker.fixtures import serve_maker, DEMO_PASSWORD


def state(base="https://example.test"):
    return dict(
        cookies=[
            dict(
                name="session",
                value="PRIVATE_SESSION_CANARY",
                domain="example.test",
                path="/",
                httpOnly=True,
                secure=True,
                sameSite="Lax",
            )
        ],
        origins=[dict(origin=base, localStorage=[dict(name="private", value="PRIVATE_STORAGE_CANARY")])],
    )


def test_lifecycle_clone_corruption_and_execution_ownership(tmp_path):
    profiles = Profiles(tmp_path / "profiles")
    first = profiles.create("Account")
    profiles.open(first).commit(state())
    second = profiles.clone(first)
    assert second != first and profiles.open(second).data["state"] == profiles.open(first).data["state"]
    profiles.clear(second)
    assert profiles.open(first).data["state"]["cookies"] and profiles.open(second).data["state"] == {}
    with FileLock(str(profiles.path(first)) + ".dispatcher-execution.lock"):
        for action in (
            lambda: profiles.clear(first),
            lambda: profiles.rename(first, "Changed"),
            lambda: profiles.delete(first),
            lambda: profiles.clone(first),
        ):
            with pytest.raises(GraphError, match="PROFILE_BUSY"):
                action()
    raw = profiles.path(first).read_bytes()
    assert b"PRIVATE_SESSION_CANARY" not in raw and b"PRIVATE_STORAGE_CANARY" not in raw
    profiles.path(first).write_bytes(raw[:-5] + b"broken")
    with pytest.raises(GraphError, match="PROFILE_INVALID"):
        profiles.open(first)
    assert profiles.open(second).data["state"] == {}


def test_origin_selection_and_unsupported_provider_refuse():
    value = state()
    value["cookies"].append(dict(name="foreign", value="foreign", domain="other.test", path="/"))
    value["origins"].append(dict(origin="https://other.test", localStorage=[]))
    selected = select_origins(value, ["https://example.test"])
    assert len(selected["cookies"]) == len(selected["origins"]) == 1
    assert not compatibility(selected, "chromium", "colab", trusted=True)["transfer_allowed"]
    assert not compatibility(selected, "unknown", "linux-agent", trusted=True)["supported"]
    assert not compatibility(selected, "chromium", "linux-agent", trusted=False)["transfer_allowed"]
    for url in [
        "file:///private",
        "https://user:password@example.test",
        "https://example.test/?token=private",
    ]:
        with pytest.raises(GraphError):
            select_origins(value, [url])
    value["cookies"][0]["partitionKey"] = "hidden"
    with pytest.raises(GraphError):
        normalize(value)


def test_target_expiry_binding_integrity_and_private_export(tmp_path):
    sender, target, other = Identity(), Identity(), Identity()
    binding = dict(run_id="run-1", version="v-1")
    packet = seal_session(
        sender,
        target.public(),
        state(),
        origins=["https://example.test"],
        trusted=True,
        binding=binding,
        now=100,
        ttl=120,
    )
    raw = json.dumps(packet).encode()
    assert b"PRIVATE_SESSION_CANARY" not in raw and b"PRIVATE_STORAGE_CANARY" not in raw
    restored = open_session(target, sender.public(), packet, binding=binding, now=101)
    assert restored["cookies"][0]["httpOnly"] and restored["cookies"][0]["secure"]
    assert restored["origins"][0]["localStorage"][0]["value"] == "PRIVATE_STORAGE_CANARY"
    for receiver, issuer, context, now in [
        (other, sender.public(), binding, 101),
        (target, other.public(), binding, 101),
        (target, sender.public(), binding, 221),
        (target, sender.public(), dict(run_id="different"), 101),
    ]:
        with pytest.raises(GraphError, match="PROFILE_PERMISSION"):
            open_session(receiver, issuer, packet, binding=context, now=now)
    damaged = dict(packet, expires=250)
    with pytest.raises(GraphError):
        open_session(target, sender.public(), damaged, binding=binding, now=101)
    path = tmp_path / "private.postersession"
    export_file(path, packet)
    assert path.read_bytes() == __import__("poster.cloud.format", fromlist=["canonical"]).canonical(packet)
    with pytest.raises(GraphError):
        export_file(path, packet)


async def test_real_browser_authorized_cookie_storage_transfer_and_clean_context(tmp_path):
    sender, target = Identity(), Identity()
    profiles = Profiles(tmp_path / "source")
    key = profiles.create("Source")
    with serve_maker() as (_, base):
        source = BrowserSession(True, profile=profiles.open(key))
        clean = BrowserSession(True)
        restored = None
        try:
            await source.start()
            await source.page.goto(base + "/login")
            await source.page.locator("#user").fill("demo")
            await source.page.locator("#password").fill(DEMO_PASSWORD)
            await source.page.locator("#login").click()
            await source.page.locator("#account").wait_for()
            await source.page.evaluate("localStorage.setItem('private','BROWSER_SESSION_CANARY')")
            await source.profile.save(source)
            packet = seal_session(
                sender, target.public(), source.profile.data["state"], origins=[base], trusted=True
            )
            imported = open_session(target, sender.public(), packet)
            destination = Profiles(tmp_path / "destination")
            new = destination.create("Transferred")
            destination.open(new).commit(imported)
            restored = BrowserSession(True, profile=destination.open(new))
            await restored.start()
            await clean.start()
            assert (await restored.page.goto(base + "/account")).status == 200
            assert await restored.page.evaluate("localStorage.getItem('private')") == "BROWSER_SESSION_CANARY"
            assert (await clean.page.goto(base + "/account")).status == 401
            assert b"BROWSER_SESSION_CANARY" not in json.dumps(packet).encode()
            assert b"BROWSER_SESSION_CANARY" not in destination.path(new).read_bytes()
        finally:
            await source.close()
            await clean.close()
            if restored:
                await restored.close()
