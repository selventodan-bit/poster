"""Explicit encrypted session snapshots, never a shared mutable browser directory."""

from pathlib import Path
from contextlib import contextmanager
import json
import re
from urllib.parse import urlsplit

from filelock import FileLock, Timeout

from ..auth import decrypt_bytes, encrypt_session
from .model import GraphError, identity
from .storage import atomic, read_bounded

RESTORE = """state=>{
 const origin=location.origin;
 for(const item of state.origins||[]) if(item.origin===origin)
   for(const entry of item.localStorage||[]) localStorage.setItem(entry.name,entry.value);
}"""


class Profiles:
    def __init__(self, root, *, key=None):
        from poster.dispatcher.security import private_dir

        try:
            self.root = private_dir(Path(root))
            from cryptography.fernet import Fernet

            self.cipher = Fernet(key) if key else None
        except Exception as exc:
            raise GraphError("PROFILE_INVALID") from exc

    def decrypt(self, raw):
        return json.loads(self.cipher.decrypt(raw)) if self.cipher else decrypt_bytes(raw)

    def encrypt(self, value):
        return self.cipher.encrypt(json.dumps(value).encode()) if self.cipher else encrypt_session(value)

    def path(self, name):
        if not re.fullmatch(r"profile_[a-f0-9]{12,32}", name):
            raise GraphError("PROFILE_INVALID")
        path = self.root / (name + ".enc")
        if path.is_symlink():
            raise GraphError("PROFILE_INVALID")
        return path

    @contextmanager
    def ownership(self, key):
        try:
            with FileLock(str(self.path(key)) + ".dispatcher-execution.lock", timeout=0):
                yield
        except Timeout as exc:
            raise GraphError("PROFILE_BUSY") from exc

    def list(self):
        result = []
        self.unreadable = []
        for path in sorted(self.root.glob("profile_*.enc")):
            try:
                value = self.decrypt(read_bounded(self.path(path.stem)))
                result.append((path.stem, value["name"]))
            except Exception:
                self.unreadable.append(path.stem)
        return result

    def create(self, name):
        if not name.strip() or len(name) > 100:
            raise GraphError("PROFILE_INVALID")
        key = "profile_" + identity()
        atomic(self.path(key), self.encrypt({"name": name, "revision": 0, "state": {}}))
        from poster.dispatcher.security import restrict

        restrict(self.path(key))
        return key

    def open(self, key):
        path = self.path(key)
        if not path.is_file():
            raise GraphError("PROFILE_MISSING")
        try:
            from poster.portability.sessions import normalize

            data = self.decrypt(read_bounded(path))
            if (
                set(data) != {"name", "revision", "state"}
                or not isinstance(data["name"], str)
                or type(data["revision"]) is not int
                or data["revision"] < 0
            ):
                raise ValueError()
            normalize(data["state"])
            return Profile(self, key, data)
        except Exception as exc:
            raise GraphError("PROFILE_INVALID") from exc

    def clone(self, key, name=None, *, clean=False):
        with self.ownership(key):
            source = self.open(key)
            created = self.create(name or source.data["name"] + " — копия")
            if not clean:
                self.open(created).commit(source.data["state"])
            return created

    def describe(self, key, *, provider="chromium", environment="windows", trusted=True):
        from poster.portability.sessions import compatibility

        profile = self.open(key)
        return dict(
            id=key,
            name=profile.data["name"],
            revision=profile.data["revision"],
            location=str(self.path(key)),
            compatibility=compatibility(profile.data["state"], provider, environment, trusted=trusted),
        )

    def clear(self, key):
        # Revision change invalidates any other session's unsaved snapshot.
        profile = self.open(key)
        profile.commit({})

    def rename(self, key, name):
        if not name.strip() or len(name) > 100:
            raise GraphError("PROFILE_INVALID")
        profile = self.open(key)
        profile.data["name"] = name.strip()
        profile.commit(profile.data["state"])

    def delete(self, key):
        path = self.path(key)
        with self.ownership(key), FileLock(str(path) + ".lock"):
            if not path.is_file():
                raise GraphError("PROFILE_MISSING")
            path.unlink()


class Profile:
    def __init__(self, store, key, data):
        self.store, self.key, self.data = store, key, data

    def commit(self, state):
        from poster.portability.sessions import normalize

        normalize(state)
        path = self.store.path(self.key)
        with self.store.ownership(self.key), FileLock(str(path) + ".lock"):
            try:
                previous = self.store.decrypt(read_bounded(path))
            except Exception as exc:
                raise GraphError("PROFILE_INVALID") from exc
            if previous["revision"] != self.data["revision"]:
                raise GraphError("PROFILE_CONFLICT")
            updated = {"name": self.data["name"], "revision": previous["revision"] + 1, "state": state}
            raw = self.store.encrypt(updated)
            if len(raw) > 10_000_000:
                raise GraphError("FILE_TOO_LARGE")
            atomic(path, raw)
            from poster.dispatcher.security import restrict

            restrict(path)
            self.data = updated

    async def restore(self, session):
        from poster.portability.sessions import prune_expired, compatibility

        state = prune_expired(self.data["state"])
        if not compatibility(state, session.provider.name, "local", trusted=True)["supported"]:
            raise GraphError("PROFILE_INCOMPATIBLE")
        cookies = state.get("cookies", [])
        if cookies:
            cdp = await session.context.new_cdp_session(session.page)
            try:
                await cdp.send("Network.setCookies", {"cookies": cookies})
            finally:
                await cdp.detach()
        # Seed an origin through a synthetic intercepted document, once per run.
        # No request reaches the website and no init script can reset later edits
        # or undo a logout's localStorage.clear(). Every tab shares this storage.
        original = session.page
        for item in state.get("origins", []):
            origin = item["origin"]
            parsed = urlsplit(origin)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.path not in {"", "/"}
                or parsed.query
                or parsed.fragment
            ):
                raise GraphError("PROFILE_INVALID")
            bootstrap = origin.rstrip("/") + "/.poster-local-bootstrap-" + identity()

            async def stub(route):
                await route.fulfill(
                    status=200, content_type="text/html", body="<title>Poster session initialization</title>"
                )

            await session.context.route(bootstrap, stub)
            page = None
            try:
                page = await session.provider.new_page()
                await page.goto(bootstrap, wait_until="domcontentloaded", timeout=10000)
                await page.evaluate(RESTORE, {"origins": [item]})
            finally:
                if page and not page.is_closed():
                    await session.provider.close_page(page)
                await session.context.unroute(bootstrap, stub)
        await session.provider.activate(original)

    async def save(self, session):
        if not session.context or not session.context.pages:
            raise GraphError("TAB_MISSING")
        cdp = await session.context.new_cdp_session(session.context.pages[0])
        try:
            raw = (await cdp.send("Network.getAllCookies"))["cookies"]
        finally:
            await cdp.detach()
        fields = {"name", "value", "domain", "path", "secure", "httpOnly", "sameSite", "expires"}
        unsupported = []
        cookies = []
        for cookie in raw:
            if cookie.get("partitionKey") or cookie.get("partitionKeyOpaque"):
                unsupported.append("partitioned-cookies")
                continue
            cookies.append({key: value for key, value in cookie.items() if key in fields})
        # Session cookies use expires=-1 in CDP; omit it when restoring.
        for cookie in cookies:
            if cookie.get("expires", -1) < 0:
                cookie.pop("expires", None)
        origins = {item["origin"]: item for item in self.data["state"].get("origins", [])}
        for page in session.context.pages:
            for frame in page.frames:
                try:
                    value = await frame.evaluate("""()=>({origin:location.origin,
                        localStorage:Object.keys(localStorage).map(name=>({name,value:localStorage.getItem(name)}))})""")
                    if value["origin"].startswith(("http://", "https://")):
                        origins[value["origin"]] = value
                except Exception:
                    pass  # Closed frames / opaque origins have no portable localStorage.
        value = dict(cookies=cookies, origins=list(origins.values()))
        if unsupported:
            value["unsupported"] = sorted(set(unsupported))
        self.commit(value)
