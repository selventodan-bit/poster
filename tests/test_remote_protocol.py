import copy
import pytest

from poster.remote.protocol import Identity, pack, unpack
from poster.dispatcher.models import DispatcherError


def test_authenticated_confidential_versioned_envelope_and_freshness():
    sender, receiver, attacker = Identity(), Identity(), Identity()
    packet = pack(sender, receiver.public(), {"secret": {"n": 3, "ok": True}}, now=100)
    assert "secret" not in str(packet)
    assert unpack(receiver, sender.public(), packet, now=101) == {"secret": {"n": 3, "ok": True}}
    for peer, now in [(attacker.public(), 101), (sender.public(), 109), (sender.public(), 90)]:
        with pytest.raises(DispatcherError):
            unpack(receiver, peer, packet, now=now)
    for field in ["protocol", "src", "dst", "cipher", "expires", "signature"]:
        broken = copy.deepcopy(packet)
        broken[field] = 0 if field in ("protocol", "expires") else "corrupted"
        with pytest.raises(DispatcherError):
            unpack(receiver, sender.public(), broken, now=101)
