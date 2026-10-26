"""Pinned Ed25519 peers, ephemeral X25519/AES-GCM envelopes, bounded freshness."""

import base64
import hashlib
import json
import os
import time
import uuid

from cryptography.hazmat.primitives.asymmetric import ed25519, x25519
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives import hashes, serialization

from poster.dispatcher.models import DispatcherError

MAX_MESSAGE = 40_000_000
LEASE = 30


def raw_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def b64(value):
    return base64.b64encode(value).decode("ascii")


def un64(value):
    return base64.b64decode(value, validate=True)


def public_key(key):
    return b64(key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw))


class Identity:
    def __init__(self, private=None):
        if private is None:
            self.sign = ed25519.Ed25519PrivateKey.generate()
            self.box = x25519.X25519PrivateKey.generate()
        else:
            try:
                if set(private) != {"sign", "box"}:
                    raise ValueError()
                self.sign = ed25519.Ed25519PrivateKey.from_private_bytes(un64(private["sign"]))
                self.box = x25519.X25519PrivateKey.from_private_bytes(un64(private["box"]))
            except Exception as exc:
                raise DispatcherError("REMOTE_IDENTITY") from exc

    def public(self):
        values = dict(sign=public_key(self.sign), box=public_key(self.box))
        return dict(id=hashlib.sha256(raw_json(values)).hexdigest(), **values)

    def private(self):
        return {
            name: b64(
                key.private_bytes(
                    serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()
                )
            )
            for name, key in [("sign", self.sign), ("box", self.box)]
        }


def validate_peer(value):
    try:
        assert set(value) == {"id", "sign", "box"}
        ed25519.Ed25519PublicKey.from_public_bytes(un64(value["sign"]))
        x25519.X25519PublicKey.from_public_bytes(un64(value["box"]))
        assert value["id"] == hashlib.sha256(raw_json({k: value[k] for k in ["sign", "box"]})).hexdigest()
        return value
    except Exception as exc:
        raise DispatcherError("REMOTE_IDENTITY") from exc


def key_for(private, public, header):
    shared = private.exchange(x25519.X25519PublicKey.from_public_bytes(un64(public)))
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=hashlib.sha256(raw_json(header)).digest(),
        info=b"poster.remote.v1",
    ).derive(shared)


def pack(identity, peer, value, *, now=None, ttl=LEASE):
    validate_peer(peer)
    now = time.time() if now is None else now
    ephemeral = x25519.X25519PrivateKey.generate()
    header = dict(
        protocol=1,
        id=uuid.uuid4().hex,
        src=identity.public()["id"],
        dst=peer["id"],
        issued=now,
        expires=now + ttl,
        ephemeral=public_key(ephemeral),
        nonce=b64(os.urandom(12)),
    )
    packet = dict(
        **header,
        cipher=b64(
            AESGCM(key_for(ephemeral, peer["box"], header)).encrypt(
                un64(header["nonce"]), raw_json(value), raw_json(header)
            )
        ),
    )
    packet["signature"] = b64(identity.sign.sign(raw_json(packet)))
    if len(raw_json(packet)) > MAX_MESSAGE:
        raise DispatcherError("REMOTE_LIMIT")
    return packet


def unpack(identity, peer, packet, *, now=None):
    try:
        if len(raw_json(packet)) > MAX_MESSAGE or set(packet) != {
            "protocol",
            "id",
            "src",
            "dst",
            "issued",
            "expires",
            "ephemeral",
            "nonce",
            "cipher",
            "signature",
        }:
            raise ValueError()
        signed = {k: v for k, v in packet.items() if k != "signature"}
        ed25519.Ed25519PublicKey.from_public_bytes(un64(peer["sign"])).verify(
            un64(packet["signature"]), raw_json(signed)
        )
        now = time.time() if now is None else now
        if (
            type(packet["protocol"]) is not int
            or packet["protocol"] != 1
            or packet["src"] != peer["id"]
            or packet["dst"] != identity.public()["id"]
        ):
            raise ValueError()
        if (
            not packet["issued"] - 3 <= now <= packet["expires"]
            or not 0 < packet["expires"] - packet["issued"] <= LEASE
        ):
            raise ValueError()
        if len(packet["id"]) != 32 or any(c not in "0123456789abcdef" for c in packet["id"]):
            raise ValueError()
        header = {k: v for k, v in signed.items() if k != "cipher"}
        value = json.loads(
            AESGCM(key_for(identity.box, packet["ephemeral"], header)).decrypt(
                un64(packet["nonce"]), un64(packet["cipher"]), raw_json(header)
            )
        )
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except Exception as exc:
        raise DispatcherError("REMOTE_PROTOCOL") from exc
