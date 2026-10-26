"""Explicit local provisioning; credentials are written to private files, never stdout."""

from datetime import datetime, timedelta, timezone
import hashlib
import ipaddress
import os
import uuid

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from poster.dispatcher.security import private_dir, safe_json, restrict
from .protocol import Identity, b64
from .registry import read_json, profile


def tls_files(root, hostname="localhost"):
    root = private_dir(root)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(x509.oid.NameOID.COMMON_NAME, hostname)])
    try:
        san = x509.IPAddress(ipaddress.ip_address(hostname))
    except ValueError:
        san = x509.DNSName(hostname)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(timezone.utc) - timedelta(minutes=5))
        .not_valid_after(datetime.now(timezone.utc) + timedelta(days=365))
        .add_extension(x509.SubjectAlternativeName([san]), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(key, hashes.SHA256())
    )
    for name, data in [
        (
            "tls.key",
            key.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
            ),
        ),
        ("tls.crt", cert.public_bytes(serialization.Encoding.PEM)),
    ]:
        path = root / name
        with path.open("xb") as stream:
            stream.write(data)
        restrict(path)
    return root / "tls.crt", root / "tls.key"


def relay_profiles(root, url, ca=None):
    root = private_dir(root)
    channel = uuid.uuid4().hex
    values = {role: b64(os.urandom(32)) for role in ["controller", "agent"]}
    for role, token in values.items():
        path = root / (role + "-relay.json")
        if path.exists():
            raise FileExistsError()
        safe_json(
            path, profile(dict(url=url, channel=channel, token=token, ca_pem=ca.read_text() if ca else None))
        )
    config = root / "relay.json"
    if config.exists():
        raise FileExistsError()
    safe_json(
        config,
        dict(
            channels={
                channel: {role: hashlib.sha256(token.encode()).hexdigest() for role, token in values.items()}
            }
        ),
    )
    return channel


def agent_init(root, name, controller_file, relay_file):
    from filelock import FileLock

    root = private_dir(root)
    with FileLock(str(root / "agent-init.lock"), timeout=0):
        return _agent_init(root, name, controller_file, relay_file)


def _agent_init(root, name, controller_file, relay_file):
    from cryptography.fernet import Fernet

    root = private_dir(root)
    if (root / "agent.json").exists():
        raise FileExistsError()
    if (root / "agent-journal.sqlite").exists() or (root / "registration.json").exists():
        raise FileExistsError()
    from .journal import Journal
    from .protocol import validate_peer

    controller = validate_peer(read_json(controller_file))
    relay = profile(read_json(relay_file))
    identity = Identity()
    storage_key = Fernet.generate_key()
    Journal(root, storage_key, create=True).close()
    safe_json(
        root / "agent.json",
        dict(
            identity=identity.private(),
            storage_key=storage_key.decode(),
            controller=controller,
            relay=relay,
            name=name,
        ),
    )
    safe_json(
        root / "registration.json",
        dict(peer=identity.public(), name=name, os="linux", version="5A.1", protocol=1),
    )
