import copy
import subprocess
import sys
import pytest

from poster.remote.protocol import Identity, pack, unpack, LEASE
from poster.dispatcher.models import DispatcherError


def test_authenticated_confidential_versioned_envelope_and_freshness():
    sender, receiver, attacker = Identity(), Identity(), Identity()
    packet = pack(sender, receiver.public(), {"secret": {"n": 3, "ok": True}}, now=100)
    assert "secret" not in str(packet)
    assert unpack(receiver, sender.public(), packet, now=101) == {"secret": {"n": 3, "ok": True}}
    for peer, now in [(attacker.public(), 101), (sender.public(), 100 + LEASE + 1), (sender.public(), 90)]:
        with pytest.raises(DispatcherError):
            unpack(receiver, peer, packet, now=now)
    for field in ["protocol", "src", "dst", "cipher", "expires", "signature"]:
        broken = copy.deepcopy(packet)
        broken[field] = 0 if field in ("protocol", "expires") else "corrupted"
        with pytest.raises(DispatcherError):
            unpack(receiver, sender.public(), broken, now=101)


def test_corrupt_saved_identity_is_rejected_instead_of_replaced():
    for private in [{}, {"sign": "broken", "box": "broken"}]:
        with pytest.raises(DispatcherError):
            Identity(private)


def test_tls_and_identity_validation_remain_enabled_with_python_optimization():
    script = """
from poster.dispatcher.models import DispatcherError
from poster.remote.protocol import Identity, validate_peer
from poster.remote.registry import profile
peer = dict(Identity().public(), id='0' * 64)
insecure = dict(url='http://relay.invalid', channel='a' * 32, token='x' * 40)
for validate, value in [(validate_peer, peer), (profile, insecure)]:
    try:
        validate(value)
    except DispatcherError:
        continue
    raise SystemExit(1)
"""
    subprocess.run([sys.executable, "-O", "-c", script], check=True, timeout=15)
