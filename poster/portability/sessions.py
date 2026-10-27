"""Supported CDP cookie/localStorage data only; never browser directories or keys."""

from copy import deepcopy
import json
import math
import time
from urllib.parse import urlsplit

from poster.maker.model import GraphError

MAX_SESSION_BYTES = 5_000_000
MAX_TRANSFER_BYTES = 512_000
COOKIE_FIELDS = {"name", "value", "domain", "path", "secure", "httpOnly", "sameSite", "expires"}
PROVIDERS = {"chromium", "qt", "embedded-qt"}


def origin(value):
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError()
        host = parsed.hostname.encode("idna").decode().lower()
        if ":" in host:
            host = "[" + host + "]"
        port = parsed.port
        suffix = ":" + str(port) if port and port != (443 if parsed.scheme == "https" else 80) else ""
        return parsed.scheme + "://" + host + suffix
    except Exception as exc:
        raise GraphError("PROFILE_INVALID") from exc


def _text(value, limit):
    if not isinstance(value, str) or len(value) > limit or any(ord(c) < 32 for c in value):
        raise GraphError("PROFILE_INVALID")
    return value


def normalize(state):
    """Accept old 3B/5C states, validate exact supported semantics, bound memory."""
    try:
        if not isinstance(state, dict) or set(state) - {"cookies", "origins", "unsupported"}:
            raise ValueError()
        if len(json.dumps(state, allow_nan=False).encode()) > MAX_SESSION_BYTES:
            raise ValueError()
        cookies, origins = state.get("cookies", []), state.get("origins", [])
        if (
            not isinstance(cookies, list)
            or len(cookies) > 3000
            or not isinstance(origins, list)
            or len(origins) > 128
        ):
            raise ValueError()
        result, seen = [], set()
        for item in cookies:
            if (
                not isinstance(item, dict)
                or set(item) - COOKIE_FIELDS
                or not {"name", "value", "domain", "path"} <= item.keys()
            ):
                raise ValueError()
            item = deepcopy(item)
            _text(item["name"], 512)
            _text(item["value"], 8192)
            domain = _text(item["domain"], 255)
            if (
                not domain
                or any(c in domain for c in "/\\@:?# ")
                or not _text(item["path"], 2048).startswith("/")
            ):
                raise ValueError()
            for field in ("secure", "httpOnly"):
                if field in item and type(item[field]) is not bool:
                    raise ValueError()
            if "sameSite" in item and item["sameSite"] not in {"Lax", "Strict", "None"}:
                raise ValueError()
            if item.get("sameSite") == "None" and not item.get("secure"):
                raise ValueError()
            if "expires" in item:
                expiry = item["expires"]
                if type(expiry) not in {int, float} or not math.isfinite(expiry):
                    raise ValueError()
                if expiry < 0:
                    item.pop("expires")  # session cookie, not a manufactured lifetime
            key = (domain, item["path"], item["name"])
            if key in seen:
                raise ValueError()
            seen.add(key)
            result.append(item)
        restored, seen = [], set()
        for entry in origins:
            if not isinstance(entry, dict) or set(entry) != {"origin", "localStorage"}:
                raise ValueError()
            value = origin(entry["origin"])
            if (
                value in seen
                or not isinstance(entry["localStorage"], list)
                or len(entry["localStorage"]) > 10000
            ):
                raise ValueError()
            seen.add(value)
            storage = []
            names = set()
            for pair in entry["localStorage"]:
                if not isinstance(pair, dict) or set(pair) != {"name", "value"}:
                    raise ValueError()
                name = _text(pair["name"], 10000)
                if name in names or not isinstance(pair["value"], str):
                    raise ValueError()
                names.add(name)
                storage.append(dict(name=name, value=pair["value"]))
            restored.append(dict(origin=value, localStorage=storage))
        unsupported = state.get("unsupported", [])
        if not isinstance(unsupported, list) or not set(unsupported) <= {
            "partitioned-cookies",
            "opaque-origin",
            "unreadable-origin",
        }:
            raise ValueError()
        value = dict(cookies=result, origins=restored)
        if unsupported:
            value["unsupported"] = sorted(set(unsupported))
        return value
    except GraphError:
        raise
    except Exception as exc:
        raise GraphError("PROFILE_INVALID") from exc


def select_origins(state, allowed):
    state = normalize(state)
    if not isinstance(allowed, list) or not 1 <= len(allowed) <= 128:
        raise GraphError("PROFILE_SCOPE")
    allowed = {origin(value) for value in allowed}
    hosts = {urlsplit(value).hostname for value in allowed}
    cookies = [
        c
        for c in state["cookies"]
        if any(
            h == c["domain"].lstrip(".").lower()
            or c["domain"].startswith(".")
            and h.endswith(c["domain"].lower())
            for h in hosts
        )
    ]
    result = dict(cookies=cookies, origins=[o for o in state["origins"] if o["origin"] in allowed])
    if state.get("unsupported"):
        result["unsupported"] = state["unsupported"]
    if len(json.dumps(result, allow_nan=False).encode()) > MAX_TRANSFER_BYTES:
        raise GraphError("PROFILE_LIMIT")
    return result


def compatibility(state, provider, environment, *, trusted=False):
    value = normalize(state)
    supported = provider in PROVIDERS
    private = bool(value["cookies"] or any(o["localStorage"] for o in value["origins"]))
    allowed = supported and (not private or environment not in {"colab", "molab"} and trusted)
    return dict(
        provider=provider,
        environment=environment,
        supported=supported,
        transfer_allowed=allowed,
        needs_reauthorization=True,
        limitations=[
            "IndexedDB",
            "sessionStorage",
            "passkeys",
            "OS-bound credentials",
            *value.get("unsupported", []),
        ],
        cookie_count=len(value["cookies"]),
        origin_count=len(value["origins"]),
    )


def prune_expired(state, *, now=None):
    value = normalize(state)
    now = time.time() if now is None else now
    value["cookies"] = [c for c in value["cookies"] if c.get("expires", now + 1) > now]
    return value
