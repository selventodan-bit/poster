"""Payload decoding and a causal response-to-request dependency graph (never time alone)."""

import html
import json
import re
from dataclasses import dataclass, field
from email import policy
from email.message import Message
from email.parser import BytesParser
from urllib.parse import parse_qsl, unquote, urlsplit

from bs4 import BeautifulSoup


def text(value):
    return " ".join(BeautifulSoup(html.unescape(str(value)), "html.parser").get_text(" ", strip=True).split())


def leaves(value, path=""):
    if isinstance(value, dict):
        for k, v in value.items():
            yield from leaves(v, f"{path}.{k}".strip("."))
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from leaves(v, f"{path}.{i}".strip("."))
    else:
        yield path, value


def decode(request):
    mime = Message()
    mime["content-type"] = request["content_type"]
    content_type = mime.get_content_type()
    charset = mime.get_content_charset() or "utf-8"
    raw = request["body"]
    if isinstance(raw, bytes):
        raw = raw.decode(charset)
    if content_type == "application/json" or content_type.endswith("+json"):
        return "json", json.loads(raw)
    if content_type == "application/x-www-form-urlencoded":
        pairs = parse_qsl(raw, keep_blank_values=True, encoding=charset, errors="strict")
        if len(dict(pairs)) != len(pairs):
            raise ValueError("Repeated fields require an explicit adapter")
        return "form", dict(pairs)
    if content_type == "multipart/form-data":
        message = BytesParser(policy=policy.default).parsebytes(
            ("Content-Type: " + request["content_type"] + "\r\nMIME-Version: 1.0\r\n\r\n").encode()
            + raw.encode(charset)
        )
        if not message.is_multipart():
            raise ValueError("Invalid multipart boundary")
        data = {}
        for part in message.iter_parts():
            name = part.get_param("name", header="content-disposition")
            if not name or part.get_filename() or name in data:
                raise ValueError("File or repeated multipart field")
            data[name] = part.get_payload(decode=True).decode(part.get_content_charset() or charset)
        return "multipart", data
    raise ValueError("Unsupported payload")


IDENTITY = re.compile(r"(^id$|_id$|uuid|nonce|token|csrf|revision|version)", re.I)


def identity_key(key):
    return bool(IDENTITY.search(re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", key).replace("-", "_")))


def rejected(response):
    if response["status"] >= 400:
        return True
    try:
        payload = json.loads(response.get("body", ""))
    except (ValueError, TypeError):
        return False
    return isinstance(payload, dict) and (payload.get("success") is False or bool(payload.get("errors")))


def response_values(response):
    """Only identity/token-like slots form a dependency, not arbitrary coincident strings."""
    try:
        for path, value in leaves(json.loads(response.get("body", ""))):
            if identity_key(path.rsplit(".", 1)[-1]) and value is not None and not isinstance(value, bool):
                yield "json_path", path, value
    except (ValueError, TypeError):
        pass
    for key, value in response.get("headers", {}).items():
        if key.lower() in {"x-csrf-token", "x-xsrf-token", "x-draft-id", "etag"}:
            yield "header", key.lower(), value


def request_values(request):
    for path, value in leaves(request.get("payload", {})):
        if identity_key(path.rsplit(".", 1)[-1]):
            yield "payload", path, value
    parsed = urlsplit(request["url"])
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        if identity_key(key):
            yield "query", key, value
    for i, segment in enumerate(parsed.path.split("/")):
        if segment:
            yield "path", str(i), unquote(segment)
    for key, value in request.get("headers", {}).items():
        if identity_key(key):
            yield "header", key, value


@dataclass(frozen=True)
class Dependency:
    parent: str
    child: str
    source_kind: str
    source_path: str
    target_kind: str
    target_path: str


@dataclass
class Analysis:
    writes: list[dict] = field(default_factory=list)
    diagnostics: list[str] = field(default_factory=list)
    linked: bool = False
    dependencies: list[Dependency] = field(default_factory=list)


class NetworkAnalyzer:
    def analyze(self, capture) -> Analysis:
        result, decoded, seeds, roles = Analysis(), [], set(), set()
        for n, request in enumerate(capture.requests):
            if request.get("blocked"):
                continue
            if request["method"] not in {"POST", "PUT", "PATCH", "DELETE"}:
                continue
            if capture.action_id and request.get("action_id") != capture.action_id:
                continue
            try:
                encoding, payload = decode(request)
            except (ValueError, UnicodeError, LookupError):
                result.diagnostics.append("UNSUPPORTED_ENCODING")
                continue
            matched = set()
            for _, value in leaves(payload):
                if not isinstance(value, str):
                    continue
                if capture.title_marker in html.unescape(value):
                    matched.add("title")
                if capture.body_marker in html.unescape(value) or (
                    text(value) == text(capture.body_marker) and text(value)
                ):
                    matched.add("body")
            item = {
                **request,
                "id": request.get("id", f"legacy-{n}"),
                "encoding": encoding,
                "payload": payload,
                "roles": matched,
            }
            decoded.append(item)
            if matched:
                seeds.add(item["id"])
                roles.update(matched)
        by_id = {w["id"]: w for w in decoded}
        positions = {w["id"]: i for i, w in enumerate(decoded)}
        for child in decoded:
            for response in capture.responses:
                parent = response.get("request_id")
                if parent not in by_id or positions[parent] >= positions[child["id"]]:
                    continue
                if not child.get("action_id") or child.get("action_id") != by_id[parent].get("action_id"):
                    continue
                if response.get("status", 500) >= 400 or response.get("time", 0) > child.get(
                    "time", float("inf")
                ):
                    continue
                for kind, path, value in response_values(response):
                    for target, slot, consumed in request_values(child):
                        if str(value) == str(consumed) and str(value):
                            result.dependencies.append(
                                Dependency(parent, child["id"], kind, path, target, slot)
                            )
        connected = set(seeds)
        while True:
            expanded = (
                connected
                | {e.parent for e in result.dependencies if e.child in connected}
                | {e.child for e in result.dependencies if e.parent in connected}
            )
            if expanded == connected:
                break
            connected = expanded
        result.writes = [w for w in decoded if w["id"] in connected]
        if not result.writes:
            result.diagnostics.append("NO_WRITE")
        if roles != {"title", "body"}:
            result.diagnostics.append("CONTENT_NOT_MAPPED")
        if len(result.writes) == 1:
            result.linked = roles == {"title", "body"}
        elif result.writes:
            # All content-bearing writes must belong to one connected causal component.
            reachable = {result.writes[0]["id"]}
            while True:
                expanded = (
                    reachable
                    | {e.parent for e in result.dependencies if e.child in reachable}
                    | {e.child for e in result.dependencies if e.parent in reachable}
                )
                if reachable == expanded:
                    break
                reachable = expanded
            result.linked = connected <= reachable and roles == {"title", "body"}
            if not result.linked:
                result.diagnostics.append("UNLINKED_REQUESTS")
        result.diagnostics = list(dict.fromkeys(result.diagnostics))
        return result
