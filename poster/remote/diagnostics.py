"""Safe observations, not readiness guesses or a second scheduling authority."""

import importlib.util
from pathlib import Path
import shutil
import sys
import time

import psutil

from poster.dispatcher.models import DispatcherError, MESSAGES
from poster.dispatcher.security import unseal
from .protocol import LEASE, Identity
from .registry import Registry, read_json, profile
from .relay import Client


def capabilities(root, *, phase="RUNNING"):
    from poster.browser import find_browser

    try:
        browser = bool(find_browser())
    except Exception:
        browser = False
    return dict(
        phase=phase,
        worker=importlib.util.find_spec("poster.remote.worker") is not None,
        browser=browser,
        disk_mb=shutil.disk_usage(root).free // 1048576,
        memory_mb=psutil.virtual_memory().available // 1048576,
    )


def validate_capabilities(value):
    if not isinstance(value, dict) or set(value) != {"phase", "worker", "browser", "disk_mb", "memory_mb"}:
        raise DispatcherError("REMOTE_PROTOCOL")
    if value["phase"] not in {"RUNNING", "STOPPED"} or any(
        type(value[k]) is not bool for k in ("worker", "browser")
    ):
        raise DispatcherError("REMOTE_PROTOCOL")
    if any(type(value[k]) is not int or not 0 <= value[k] < 2**50 for k in ("disk_mb", "memory_mb")):
        raise DispatcherError("REMOTE_PROTOCOL")
    return value


def doctor(root, role, *, network=False):
    root = Path(root)
    codes = []
    if sys.platform != "linux" or sys.version_info[:2] not in {(3, 11), (3, 12), (3, 13)}:
        codes.append("REMOTE_CONFIG")
    result = dict(
        role=role,
        configured=False,
        codes=codes,
        platform=sys.platform,
        python=".".join(map(str, sys.version_info[:3])),
    )
    try:
        if role == "agent":
            from .journal import Journal
            from .protocol import validate_peer

            config = read_json(root / "agent.json")
            identity = Identity(config["identity"])
            validate_peer(config["controller"])
            Journal(root, config["storage_key"].encode()).close()
            result.update(id=identity.public()["id"], capabilities=capabilities(root))
            relay = profile(config["relay"])
            for key, code in [("worker", "WORKER_UNAVAILABLE"), ("browser", "BROWSER_UNAVAILABLE")]:
                if not result["capabilities"][key]:
                    codes.append(code)
            if result["capabilities"]["disk_mb"] < 64:
                codes.append("REMOTE_DISK")
            if result["capabilities"]["memory_mb"] < 256:
                codes.append("REMOTE_MEMORY")
        else:
            import ssl
            import sqlite3

            config = read_json(root / "relay.json")
            if not config["channels"]:
                raise DispatcherError("REMOTE_CONFIG")
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(root / "tls.crt", root / "tls.key")
            path = root / "mailbox.sqlite"
            if path.exists():
                with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
                    if (
                        db.execute("PRAGMA application_id").fetchone()[0] != 0x50524C59
                        or db.execute("PRAGMA quick_check").fetchone()[0] != "ok"
                    ):
                        raise DispatcherError("REMOTE_CONFIG")
            relay = None
        result["configured"] = True
        if network and relay:
            client = Client(relay, "agent")
            try:
                result.update(client.probe())
            finally:
                client.close()
    except Exception as exc:
        codes.append(exc.code if isinstance(exc, DispatcherError) else "REMOTE_CONFIG")
    result["ready"] = result["configured"] and not any(
        c not in {"BROWSER_UNAVAILABLE", "REMOTE_MEMORY"} for c in codes
    )
    return result


def connection(store, agent_id, settings):
    row = store.db.execute("SELECT * FROM remote_agents WHERE id=?", (agent_id,)).fetchone()
    if row is None:
        raise DispatcherError("REMOTE_IDENTITY")
    observed = next(r for r in Registry(store).rows() if r["id"] == agent_id)
    codes = []
    result = dict(
        id=agent_id, online=observed["online"], last_seen=row["last_seen"], relay=False, tls_verified=False
    )
    try:
        client = Client(unseal(row["relay_cipher"]), "controller")
        try:
            result.update(client.probe())
        finally:
            client.close()
    except DispatcherError as exc:
        codes.append(exc.code)
    if row["state"] != "TRUSTED":
        codes.append("REMOTE_REVOKED" if row["state"] == "REVOKED" else "REMOTE_IDENTITY")
    if row["protocol"] != 1:
        codes.append("REMOTE_VERSION")
    caps = observed["capabilities"]
    if caps.get("phase") == "STOPPED":
        codes.append("REMOTE_STOPPED")
    elif not observed["online"]:
        codes.append(
            "REMOTE_STALE"
            if row["last_seen"] and time.time() - row["last_seen"] >= LEASE
            else "REMOTE_OFFLINE"
        )
    if observed["online"]:
        for key, code in [("worker", "WORKER_UNAVAILABLE"), ("browser", "BROWSER_UNAVAILABLE")]:
            if caps.get(key) is False:
                codes.append(code)
        for key, minimum, code in [("disk_mb", 64, "REMOTE_DISK"), ("memory_mb", 256, "REMOTE_MEMORY")]:
            if key in caps and caps[key] < minimum:
                codes.append(code)
    active = len(store.active())
    result["free_slots"] = max(0, settings["max_workers"] - active)
    if not result["free_slots"]:
        codes.append("REMOTE_NO_SLOTS")
    latest = store.db.execute(
        "SELECT e.code FROM executions e JOIN remote_runs r ON r.run_id=e.run_id WHERE r.agent_id=? ORDER BY e.leased DESC LIMIT 1",
        (agent_id,),
    ).fetchone()
    if latest and latest[0] in {"DISPATCHER_LOST", "REPORT_INVALID", "WORKER_CRASH"}:
        codes.append("REMOTE_EXPIRED" if latest[0] == "DISPATCHER_LOST" else "REMOTE_RESULT")
    if row["last_error"] in MESSAGES and row["last_error"] not in codes:
        codes.append(row["last_error"])
    result.update(codes=codes, messages=[MESSAGES[c] for c in codes], capabilities=caps)
    return result
