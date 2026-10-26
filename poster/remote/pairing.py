"""Private file pairing over existing 5A pins; no new network pairing service."""

import json
import time
import uuid

from filelock import FileLock
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from poster.dispatcher.models import DispatcherError
from poster.dispatcher.security import private_dir, safe_json, seal, unseal
from .protocol import Identity, b64, un64, raw_json, validate_peer
from .registry import Registry, read_json, profile

PAIRING_TTL = 900


def export_offer(store, operator_file, url, destination):
    data = read_json(operator_file)
    controller, agent = profile(data["controller"]), profile(data["agent"])
    if (
        controller["url"].rstrip("/") != url.rstrip("/")
        or any(controller[k] != agent[k] for k in ("url", "channel"))
        or controller["token"] == agent["token"]
    ):
        raise DispatcherError("REMOTE_CONFIG")
    from .relay import Client

    client = Client(controller, "controller")
    try:
        client.probe()
    finally:
        client.close()
    peer = Registry(store).identity().public()
    nonce, expiry = uuid.uuid4().hex, time.time() + PAIRING_TTL
    offer = dict(format="poster.pairing.v1", id=nonce, expires=expiry, controller=peer, relay=agent)
    safe_json(destination, offer)
    store.db.execute(
        "INSERT INTO remote_pairings VALUES(?,?,?,?,NULL)", (nonce, expiry, peer["id"], seal(controller))
    )
    return dict(controller=peer["id"], expires=expiry, id=nonce)


def agent_pair(root, name, offer_file, expected_controller):
    from .setup import _agent_init

    offer = read_json(offer_file)
    peer = validate_peer(offer["controller"])
    if peer["id"] != expected_controller:
        raise DispatcherError("REMOTE_FINGERPRINT")
    if (
        offer.get("format") != "poster.pairing.v1"
        or not time.time() < offer["expires"] <= time.time() + PAIRING_TTL + 3
    ):
        raise DispatcherError("REMOTE_PAIRING")
    if len(offer["id"]) != 32 or any(c not in "0123456789abcdef" for c in offer["id"]):
        raise DispatcherError("REMOTE_PAIRING")
    root = private_dir(root)
    with FileLock(str(root / "agent-init.lock"), timeout=0):
        # Reuse the exact 5A initialization and replay journal. Never reset one.
        _agent_init(root, name, None, None, controller=peer, relay=profile(offer["relay"]))
        config = read_json(root / "agent.json")
        registration = read_json(root / "registration.json")
        binding = dict(
            id=offer["id"], expires=offer["expires"], controller=peer["id"], agent=registration["peer"]["id"]
        )
        registration["pairing"] = dict(
            **binding, signature=b64(Identity(config["identity"]).sign.sign(raw_json(binding)))
        )
        safe_json(root / "registration.json", registration)
    return registration["peer"]["id"]


def finish_offer(store, registration_file, expected_agent):
    registration = read_json(registration_file)
    peer = validate_peer(registration["peer"])
    if peer["id"] != expected_agent:
        raise DispatcherError("REMOTE_FINGERPRINT")
    try:
        binding = dict(registration["pairing"])
        signature = binding.pop("signature")
        Ed25519PublicKey.from_public_bytes(un64(peer["sign"])).verify(un64(signature), raw_json(binding))
        with FileLock(str(store.path) + ".pairing.lock", timeout=0):
            row = store.db.execute("SELECT * FROM remote_pairings WHERE id=?", (binding["id"],)).fetchone()
            if (
                row is None
                or row["consumed"]
                or time.time() >= row["expires"]
                or binding
                != dict(id=row["id"], expires=row["expires"], controller=row["controller"], agent=peer["id"])
            ):
                raise ValueError()
            previous = store.db.execute(
                "SELECT peer,state FROM remote_agents WHERE id=?", (peer["id"],)
            ).fetchone()
            if previous:
                if previous["state"] != "PENDING" or json.loads(previous["peer"]) != peer:
                    raise ValueError()
                result = peer["id"]  # Recovery after commit of registration but before nonce consumption.
            else:
                result = Registry(store).register(registration, unseal(row["profile_cipher"]))
            store.db.execute("UPDATE remote_pairings SET consumed=? WHERE id=?", (result, row["id"]))
            return result
    except Exception as exc:
        if isinstance(exc, DispatcherError) and exc.code in {
            "REMOTE_FINGERPRINT",
            "REMOTE_CHANNEL",
            "REMOTE_LIMIT",
        }:
            raise
        raise DispatcherError("REMOTE_PAIRING") from exc
