"""Desktop OAuth PKCE + user-bound credential protection, never a bundled OAuth app."""

import asyncio
import base64
import ctypes
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import secrets
import threading
from urllib.parse import parse_qs, urlencode, urlsplit
import webbrowser

from cryptography.fernet import Fernet
from filelock import FileLock
import httpx

from poster.dispatcher.security import plain_path, private_dir, restrict
from poster.maker.storage import atomic
from poster.storage.drive import GoogleToken
from .errors import CloudError

# drive.file covers app-created project files. Read-only is additionally needed
# for results created by Colab/Molab's different OAuth clients in the same account.
SCOPES = ["https://www.googleapis.com/auth/drive.file", "https://www.googleapis.com/auth/drive.readonly"]
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"


def default_root():
    if os.environ.get("POSTER_CLOUD_HOME"):
        return Path(os.environ["POSTER_CLOUD_HOME"])
    return (
        Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Poster3/Cloud"
        if os.name == "nt"
        else Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "poster3/cloud"
    )


def dpapi(raw, *, decrypt=False):
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]

    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    function.argtypes = [
        ctypes.POINTER(Blob),
        ctypes.c_void_p,
        ctypes.POINTER(Blob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(Blob),
    ]
    function.restype = wintypes.BOOL
    buffer = ctypes.create_string_buffer(raw)
    entropy = ctypes.create_string_buffer(b"Poster3-cloud-user-v1")
    source = Blob(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    extra = Blob(len(entropy.raw) - 1, ctypes.cast(entropy, ctypes.POINTER(ctypes.c_ubyte)))
    target = Blob()
    if not function(ctypes.byref(source), None, ctypes.byref(extra), None, None, 1, ctypes.byref(target)):
        raise CloudError("AUTH_REQUIRED")
    try:
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        kernel.LocalFree(ctypes.cast(target.pbData, ctypes.c_void_p))


class SecretStore:
    """Windows DPAPI per user. Other OSes: private local Fernet for explicit hosts."""

    def __init__(self, root=None):
        from ..product.locking import connection_lease

        self.root = plain_path(Path(root) if root else default_root())
        self._data_lease = connection_lease(self.root)
        try:
            private_dir(self.root)
        except BaseException:
            self._data_lease.close()
            raise
        self.path = self.root / "credentials.bin"
        self.lock = FileLock(str(self.root / "credentials.lock"))

    def close(self):
        lease = getattr(self, "_data_lease", None)
        if lease:
            lease.close()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    def _ensure_open(self):
        if not self._data_lease.owned:
            from ..product.locking import connection_lease

            self._data_lease = connection_lease(self.root)
            private_dir(self.root)

    def _cipher(self):
        path = plain_path(self.root / "credentials.key")
        if not path.exists():
            if self.path.exists():
                raise CloudError("AUTH_REQUIRED")
            try:
                with path.open("xb") as stream:
                    stream.write(Fernet.generate_key())
                restrict(path)
            except FileExistsError:
                pass
        if path.is_symlink():
            raise CloudError("AUTH_REQUIRED")
        if path.stat().st_size > 4096:
            raise CloudError("AUTH_REQUIRED")
        return Fernet(path.read_bytes())

    def _read(self):
        if not self.path.exists():
            return {}
        try:
            if self.path.is_symlink() or self.path.stat().st_size > 1_000_000:
                raise ValueError()
            raw = self.path.read_bytes()
            if os.name == "nt":
                if not raw.startswith(b"PSTDP1\0"):
                    raise ValueError()
                plain = dpapi(raw[7:], decrypt=True)
            else:
                if not raw.startswith(b"PSTFN1\0"):
                    raise ValueError()
                plain = self._cipher().decrypt(raw[7:])
            result = json.loads(plain)
            if not isinstance(result, dict):
                raise ValueError()
            return result
        except Exception as exc:
            raise CloudError("AUTH_REQUIRED") from exc

    def get(self, name):
        self._ensure_open()
        with self.lock:
            return self._read().get(name)

    def get_or_create(self, name, factory):
        self._ensure_open()
        with self.lock:
            value = self._read().get(name)
            if value is None:
                value = factory()
                self.put(name, value)
            return value

    def put(self, name, value):
        self._ensure_open()
        with self.lock:
            data = self._read()
            if value is None:
                data.pop(name, None)
            else:
                data[name] = value
            if not data:
                self.path.unlink(missing_ok=True)
                return
            raw = json.dumps(data, ensure_ascii=False, allow_nan=False).encode()
            protected = (
                b"PSTDP1\0" + dpapi(raw) if os.name == "nt" else b"PSTFN1\0" + self._cipher().encrypt(raw)
            )
            atomic(self.path, protected)
            restrict(self.path)


class DesktopOAuth:
    def __init__(self, store=None, *, client=None, open_browser=None):
        self.store = store or SecretStore()
        self.client = client
        self.open_browser = open_browser or webbrowser.open
        self.cancelled = threading.Event()
        self.callback_url = None
        self._connecting = False

    def configure(self, path):
        try:
            raw = Path(path).read_bytes()
            if len(raw) > 100_000:
                raise ValueError()
            value = json.loads(raw)["installed"]
            client_id = value["client_id"]
            if (
                not isinstance(client_id, str)
                or not client_id.endswith(".apps.googleusercontent.com")
                or len(client_id) > 500
            ):
                raise ValueError()
            if value.get("token_uri", TOKEN_URL) != TOKEN_URL:
                raise ValueError()
            if value.get("auth_uri", AUTH_URL) not in {AUTH_URL, "https://accounts.google.com/o/oauth2/auth"}:
                raise ValueError()
            config = dict(client_id=client_id, client_secret=value.get("client_secret", ""))
            if not isinstance(config["client_secret"], str) or len(config["client_secret"]) > 4096:
                raise ValueError()
            previous = self.store.get("oauth-client")
            if previous and previous.get("client_id") != config["client_id"]:
                self.store.put("oauth", None)
            self.store.put("oauth-client", config)
        except Exception as exc:
            raise CloudError("AUTH_CONFIG_REQUIRED") from exc

    def status(self):
        return dict(
            configured=bool(self.store.get("oauth-client")),
            connected=bool(self.store.get("oauth")),
            protected_by="Windows DPAPI" if os.name == "nt" else "private local Fernet",
        )

    def cancel(self):
        self.cancelled.set()

    async def connect(self, *, timeout=180):
        if self._connecting:
            raise CloudError("AUTH_CANCELLED")
        self._connecting = True
        try:
            return await self._connect(timeout=timeout)
        finally:
            self._connecting = False

    async def _connect(self, *, timeout):
        config = self.store.get("oauth-client")
        if not config:
            raise CloudError("AUTH_CONFIG_REQUIRED")
        self.cancelled.clear()
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        event, result = threading.Event(), {}
        expected_host = None

        class Callback(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                if event.is_set():
                    self.send_error(409, "Authorization callback already consumed")
                    return
                parsed = urlsplit(self.path)
                fields = parse_qs(parsed.query)
                if (
                    parsed.path != "/callback"
                    or self.headers.get("Host") != expected_host
                    or len(self.path) > 16000
                    or not hmac.compare_digest(fields.get("state", [""])[0], state)
                ):
                    self.send_error(400, "Authorization callback rejected")
                    return
                code = fields.get("code", [None])[0]
                if code and len(code) <= 8192:
                    result["code"] = code
                else:
                    result["error"] = True
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write("<p>Авторизация завершена. Вернитесь в Poster.</p>".encode())
                event.set()

        server = HTTPServer(("127.0.0.1", 0), Callback)
        expected_host = "127.0.0.1:" + str(server.server_port)
        redirect = "http://" + expected_host + "/callback"
        self.callback_url = redirect
        thread = threading.Thread(target=server.serve_forever, name="poster-oauth-callback", daemon=True)
        thread.start()
        url = (
            AUTH_URL
            + "?"
            + urlencode(
                dict(
                    client_id=config["client_id"],
                    redirect_uri=redirect,
                    response_type="code",
                    scope=" ".join(SCOPES),
                    state=state,
                    code_challenge=challenge,
                    code_challenge_method="S256",
                    access_type="offline",
                    prompt="consent",
                )
            )
        )
        try:
            await asyncio.to_thread(self.open_browser, url)
            deadline = asyncio.get_running_loop().time() + timeout
            while not event.is_set():
                if self.cancelled.is_set() or asyncio.get_running_loop().time() > deadline:
                    raise CloudError("AUTH_CANCELLED")
                await asyncio.sleep(0.1)
            if result.get("error"):
                raise CloudError("AUTH_CANCELLED")
            client = self.client or httpx.AsyncClient(timeout=30, follow_redirects=False)
            try:
                response = await client.post(
                    TOKEN_URL,
                    data=dict(
                        grant_type="authorization_code",
                        client_id=config["client_id"],
                        client_secret=config["client_secret"],
                        redirect_uri=redirect,
                        code=result["code"],
                        code_verifier=verifier,
                    ),
                )
                if response.status_code != 200:
                    raise CloudError("AUTH_REQUIRED")
                data = response.json()
                if not isinstance(data.get("access_token"), str) or not isinstance(
                    data.get("refresh_token"), str
                ):
                    raise CloudError("AUTH_REQUIRED")
                if "scope" in data and not set(SCOPES) <= set(data["scope"].split()):
                    raise CloudError("AUTH_REQUIRED")
                from google.oauth2.credentials import Credentials

                credentials = Credentials(
                    token=data["access_token"],
                    refresh_token=data["refresh_token"],
                    token_uri=TOKEN_URL,
                    client_id=config["client_id"],
                    client_secret=config["client_secret"],
                    scopes=SCOPES,
                )
                credentials.expiry = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(
                    seconds=max(1, min(int(data.get("expires_in", 3600)), 86400))
                )
                self.store.put("oauth", json.loads(credentials.to_json()))
                return dict(connected=True)
            finally:
                if self.client is None:
                    await client.aclose()
        except CloudError:
            raise
        except Exception as exc:
            raise CloudError("AUTH_REQUIRED") from exc
        finally:
            await asyncio.to_thread(server.shutdown)
            server.server_close()
            thread.join(3)
            self.callback_url = None

    def token(self):
        from google.oauth2.credentials import Credentials

        data = self.store.get("oauth")
        if not data:
            raise CloudError("AUTH_REQUIRED")
        credentials = Credentials.from_authorized_user_info(data, scopes=SCOPES)
        original = GoogleToken(credential=credentials, scopes=SCOPES)
        saved = credentials.to_json()
        store = self.store

        async def token():
            nonlocal saved
            try:
                current = self.store.get("oauth")
                if not current or current.get("refresh_token") != credentials.refresh_token:
                    raise CloudError("AUTH_REQUIRED")
                value = await original()
                encoded = credentials.to_json()
                if encoded != saved:
                    await asyncio.to_thread(store.put, "oauth", json.loads(encoded))
                    saved = encoded
                return value
            except Exception as exc:
                raise CloudError("AUTH_REQUIRED") from exc

        return token

    async def disconnect(self):
        value = self.store.get("oauth")
        revoked = not value
        try:
            if value:
                client = self.client or httpx.AsyncClient(timeout=10, follow_redirects=False)
                try:
                    response = await client.post(
                        REVOKE_URL, data={"token": value.get("refresh_token") or value.get("token")}
                    )
                    revoked = response.status_code == 200
                finally:
                    if self.client is None:
                        await client.aclose()
        except Exception:
            revoked = False
        finally:
            self.store.put("oauth", None)
            self.store.put("oauth-client", None)
        return dict(local_removed=True, revoked=revoked)
