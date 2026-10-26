"""Real loopback PKCE callback + real OS vault; token endpoint is a controlled fixture."""

import base64
import hashlib
import json
import urllib.error
import urllib.parse
import urllib.request

import httpx
import pytest

from poster.cloud.auth import DesktopOAuth, SecretStore, SCOPES, TOKEN_URL
from poster.cloud.crypto import decrypt_output, encrypt_output, recipient, sign_result, verify_result
from poster.cloud.errors import CloudError
from poster.remote.protocol import Identity


def config(tmp_path):
    path = tmp_path / "desktop-client.json"
    path.write_text(
        json.dumps(
            {
                "installed": {
                    "client_id": "FIXTURE.apps.googleusercontent.com",
                    "client_secret": "DESKTOP_PUBLIC_FIXTURE",
                    "token_uri": TOKEN_URL,
                }
            }
        )
    )
    return path


def test_user_vault_ciphertext_and_recipient_survive_auth_disconnect(tmp_path):
    store = SecretStore(tmp_path / "secure")
    store.put("oauth", {"refresh_token": "PRIVATE_REFRESH_CANARY"})
    identity = recipient(store)
    assert b"PRIVATE_REFRESH_CANARY" not in store.path.read_bytes()
    assert identity.public() == recipient(SecretStore(tmp_path / "secure")).public()
    store.put("oauth", None)
    assert store.get("oauth") is None
    assert recipient(store).public() == identity.public()
    raw = bytearray(store.path.read_bytes())
    raw[-7] ^= 1
    store.path.write_bytes(raw)
    with pytest.raises(CloudError, match="AUTH_REQUIRED"):
        store.get("result-identity")


async def test_pkce_loopback_rejects_wrong_state_and_never_stores_plain_tokens(tmp_path):
    observed = {}

    def browser(url):
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        observed.update(query)
        assert query["code_challenge_method"] == ["S256"]
        assert set(query["scope"][0].split()) == set(SCOPES)
        redirect = query["redirect_uri"][0]
        bad = redirect + "?" + urllib.parse.urlencode({"state": "wrong", "code": "IGNORED_CODE"})
        try:
            urllib.request.build_opener(urllib.request.ProxyHandler({})).open(bad, timeout=5)
        except urllib.error.HTTPError as exc:
            assert exc.code == 400
        else:
            raise AssertionError("Wrong state accepted")
        good = (
            redirect
            + "?"
            + urllib.parse.urlencode({"state": query["state"][0], "code": "ONE_TIME_FIXTURE_CODE"})
        )
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(good, timeout=5) as response:
            body = response.read()
            assert b"ONE_TIME_FIXTURE_CODE" not in body
            assert response.status == 200

    def exchange(request):
        assert str(request.url) == TOKEN_URL
        fields = urllib.parse.parse_qs(request.content.decode())
        assert fields["code"] == ["ONE_TIME_FIXTURE_CODE"]
        verifier = fields["code_verifier"][0]
        expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        assert observed["code_challenge"] == [expected]
        return httpx.Response(
            200,
            json={
                "access_token": "PRIVATE_ACCESS_CANARY",
                "refresh_token": "PRIVATE_REFRESH_CANARY",
                "expires_in": 3600,
                "scope": " ".join(SCOPES),
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(exchange)) as client:
        store = SecretStore(tmp_path / "vault")
        oauth = DesktopOAuth(store, client=client, open_browser=browser)
        oauth.configure(config(tmp_path))
        assert (await oauth.connect(timeout=10))["connected"]
        assert oauth.callback_url is None
        assert await oauth.token()() == "PRIVATE_ACCESS_CANARY"
        assert b"PRIVATE_ACCESS_CANARY" not in store.path.read_bytes()
        assert b"PRIVATE_REFRESH_CANARY" not in store.path.read_bytes()
        assert "code_verifier" not in json.dumps(store.get("oauth"))


async def test_disconnect_removes_authorization_even_when_google_unavailable(tmp_path):
    def unavailable(_):
        return httpx.Response(503)

    store = SecretStore(tmp_path / "vault")
    store.put("oauth", {"refresh_token": "PRIVATE_REFRESH_CANARY"})
    store.put("oauth-client", {"client_id": "not-a-production-app"})
    identity = recipient(store).public()
    async with httpx.AsyncClient(transport=httpx.MockTransport(unavailable)) as client:
        result = await DesktopOAuth(store, client=client).disconnect()
    assert result == {"local_removed": True, "revoked": False}
    assert store.get("oauth") is None and store.get("oauth-client") is None
    assert recipient(store).public() == identity


def test_web_client_cannot_be_mistaken_for_desktop_application(tmp_path):
    path = tmp_path / "web.json"
    path.write_text(json.dumps({"web": {"client_id": "PRIVATE_WEB"}}))
    with pytest.raises(CloudError, match="AUTH_CONFIG_REQUIRED"):
        DesktopOAuth(SecretStore(tmp_path / "vault")).configure(path)


def test_offline_result_encryption_binds_recipient_run_version_and_signature():
    destination, other, issuer = Identity(), Identity(), Identity()
    context = dict(project_id="project", version="0" * 64, run_id="run", path="out.txt")
    encrypted = encrypt_output(destination.public(), b"PRIVATE_OUTPUT_CANARY", context)
    assert b"PRIVATE_OUTPUT_CANARY" not in encrypted
    assert decrypt_output(destination, encrypted, context) == b"PRIVATE_OUTPUT_CANARY"
    with pytest.raises(CloudError, match="RESULT_UNTRUSTED"):
        decrypt_output(other, encrypted, context)
    with pytest.raises(CloudError, match="RESULT_UNTRUSTED"):
        decrypt_output(destination, encrypted, {**context, "run_id": "other"})
    result = sign_result(
        issuer,
        dict(
            format="poster.cloud.result.v1",
            mode="DETACHED",
            issuer=issuer.public()["id"],
            run_id="run",
            status="SUCCEEDED",
        ),
    )
    assert verify_result(issuer.public(), result)["status"] == "SUCCEEDED"
    with pytest.raises(CloudError, match="RESULT_UNTRUSTED"):
        verify_result(issuer.public(), {**result, "status": "UNKNOWN"})
    with pytest.raises(CloudError, match="RESULT_UNTRUSTED"):
        verify_result(other.public(), result)
