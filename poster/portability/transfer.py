"""Target-bound, expiring session exports over existing identity/box primitives."""

import json
import time
import uuid

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from poster.cloud.crypto import encrypt_output, decrypt_output
from poster.cloud.format import canonical, digest
from poster.dispatcher.security import plain_path, restrict
from poster.maker.model import GraphError
from poster.maker.storage import atomic
from poster.remote.protocol import b64, un64, validate_peer
from .sessions import select_origins, normalize, compatibility, MAX_TRANSFER_BYTES

MAX_CAPSULE_BYTES = 2_000_000


def seal_session(
    identity,
    peer,
    state,
    *,
    origins,
    environment="linux-agent",
    trusted=False,
    provider="chromium",
    ttl=120,
    binding=None,
    now=None,
):
    validate_peer(peer)
    state = select_origins(state, origins)
    if not compatibility(state, provider, environment, trusted=trusted)["transfer_allowed"]:
        raise GraphError("PROFILE_INCOMPATIBLE")
    if type(ttl) is not int or not 5 <= ttl <= 900:
        raise GraphError("PROFILE_PERMISSION")
    now = time.time() if now is None else now
    header = dict(
        format="poster.browser-transfer.v1",
        src=identity.public()["id"],
        dst=peer["id"],
        id=uuid.uuid4().hex,
        issued=now,
        expires=now + ttl,
        environment=environment,
        provider=provider,
        binding=binding or {},
    )
    raw = canonical(dict(state=state, sha256=digest(canonical(state))))
    value = dict(**header, box=json.loads(encrypt_output(peer, raw, header)))
    value["signature"] = b64(identity.sign.sign(canonical(value)))
    if len(canonical(value)) > MAX_CAPSULE_BYTES:
        raise GraphError("PROFILE_LIMIT")
    return value


def open_session(
    identity, peer, packet, *, environment="linux-agent", provider="chromium", binding=None, now=None
):
    try:
        validate_peer(peer)
        if len(canonical(packet)) > MAX_CAPSULE_BYTES or set(packet) != {
            "format",
            "src",
            "dst",
            "id",
            "issued",
            "expires",
            "environment",
            "provider",
            "binding",
            "box",
            "signature",
        }:
            raise ValueError()
        value = {k: v for k, v in packet.items() if k != "signature"}
        Ed25519PublicKey.from_public_bytes(un64(peer["sign"])).verify(
            un64(packet["signature"]), canonical(value)
        )
        now = time.time() if now is None else now
        if (
            packet["format"] != "poster.browser-transfer.v1"
            or packet["src"] != peer["id"]
            or packet["dst"] != identity.public()["id"]
            or not packet["issued"] - 3 <= now <= packet["expires"]
            or not 5 <= packet["expires"] - packet["issued"] <= 900
            or packet["environment"] != environment
            or packet["binding"] != (binding or {})
        ):
            raise ValueError()
        if provider not in {"chromium", "qt", "embedded-qt"} or packet["provider"] not in {
            "chromium",
            "qt",
            "embedded-qt",
        }:
            raise ValueError()
        header = {k: v for k, v in value.items() if k != "box"}
        raw = decrypt_output(identity, canonical(packet["box"]), header)
        if len(raw) > MAX_TRANSFER_BYTES + 512:
            raise ValueError()
        data = json.loads(raw)
        state = normalize(data["state"])
        if data["sha256"] != digest(canonical(state)):
            raise ValueError()
        if not compatibility(state, provider, environment, trusted=True)["transfer_allowed"]:
            raise ValueError()
        return state
    except Exception as exc:
        raise GraphError("PROFILE_PERMISSION") from exc


def export_file(path, packet):
    path = plain_path(path)
    if path.exists():
        raise GraphError("PROFILE_PERMISSION")
    raw = canonical(packet)
    if len(raw) > MAX_CAPSULE_BYTES:
        raise GraphError("PROFILE_LIMIT")
    atomic(path, raw)
    restrict(path)
    return dict(file=str(path), recipient=packet["dst"], expires=packet["expires"])
