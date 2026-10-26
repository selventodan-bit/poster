"""Offline result encryption with an independent recipient, never Windows queue.key."""

import os

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ed25519, x25519
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from poster.remote.protocol import Identity, b64, un64, public_key, validate_peer
from .errors import CloudError
from .format import canonical


def recipient(store):
    value = store.get_or_create("result-identity", lambda: Identity().private())
    identity = Identity(value)
    return identity


def _key(shared, header):
    return HKDF(
        algorithm=hashes.SHA256(), length=32, salt=None, info=b"poster.cloud.result.v1\0" + canonical(header)
    ).derive(shared)


def encrypt_output(peer, raw, context):
    validate_peer(peer)
    ephemeral = x25519.X25519PrivateKey.generate()
    header = dict(
        format="poster.cloud.result.box.v1",
        context=context,
        recipient=peer["id"],
        ephemeral=public_key(ephemeral),
        nonce=b64(os.urandom(12)),
    )
    shared = ephemeral.exchange(x25519.X25519PublicKey.from_public_bytes(un64(peer["box"])))
    return canonical(
        dict(
            **header,
            cipher=b64(AESGCM(_key(shared, header)).encrypt(un64(header["nonce"]), raw, canonical(header))),
        )
    )


def decrypt_output(identity, value, context):
    try:
        import json

        if len(value) > 30_000_000:
            raise ValueError()
        value = json.loads(value)
        if (
            set(value) != {"format", "context", "recipient", "ephemeral", "nonce", "cipher"}
            or value["format"] != "poster.cloud.result.box.v1"
            or value["context"] != context
            or value["recipient"] != identity.public()["id"]
        ):
            raise ValueError()
        header = {k: v for k, v in value.items() if k != "cipher"}
        shared = identity.box.exchange(x25519.X25519PublicKey.from_public_bytes(un64(value["ephemeral"])))
        return AESGCM(_key(shared, header)).decrypt(
            un64(value["nonce"]), un64(value["cipher"]), canonical(header)
        )
    except Exception as exc:
        raise CloudError("RESULT_UNTRUSTED") from exc


def sign_result(identity, body):
    if body.get("mode") != "DETACHED":
        raise CloudError("RESULT_UNTRUSTED")
    return dict(**body, signature=b64(identity.sign.sign(canonical(body))))


def verify_result(peer, value):
    try:
        validate_peer(peer)
        if value.get("mode") != "DETACHED" or value.get("issuer") != peer["id"]:
            raise ValueError()
        body = {k: v for k, v in value.items() if k != "signature"}
        ed25519.Ed25519PublicKey.from_public_bytes(un64(peer["sign"])).verify(
            un64(value["signature"]), canonical(body)
        )
        return body
    except Exception as exc:
        raise CloudError("RESULT_UNTRUSTED") from exc
