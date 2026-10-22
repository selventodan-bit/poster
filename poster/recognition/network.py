"""Decode payloads before correlation. Timing alone never establishes a multi-write workflow."""

import html
import json
from dataclasses import dataclass, field
from email import policy
from email.parser import BytesParser
from urllib.parse import parse_qsl

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
    content_type = request["content_type"].lower()
    raw = request["body"]
    if "application/json" in content_type:
        return "json", json.loads(raw)
    if "application/x-www-form-urlencoded" in content_type:
        pairs = parse_qsl(raw, keep_blank_values=True)
        if len(dict(pairs)) != len(pairs):
            raise ValueError("Repeated fields")
        return "form", dict(pairs)
    if "multipart/form-data" in content_type:
        message = BytesParser(policy=policy.default).parsebytes(
            ("Content-Type: " + request["content_type"] + "\r\nMIME-Version: 1.0\r\n\r\n").encode()
            + raw.encode()
        )
        data = {}
        for part in message.iter_parts():
            name = part.get_param("name", header="content-disposition")
            if not name or part.get_filename() or name in data:
                raise ValueError("File or repeated multipart field")
            data[name] = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8")
        return "multipart", data
    raise ValueError("Unsupported payload")


@dataclass
class Analysis:
    writes: list[dict] = field(default_factory=list)
    diagnostics: list[str] = field(default_factory=list)
    linked: bool = False


class NetworkAnalyzer:
    def analyze(self, capture) -> Analysis:
        result, roles = Analysis(), set()
        for request in capture.requests:
            if request["method"] not in {"POST", "PUT", "PATCH", "DELETE"}:
                continue
            if capture.action_id and request.get("action_id") != capture.action_id:
                continue
            try:
                encoding, payload = decode(request)
            except (ValueError, UnicodeError):
                if request.get("action_id") == capture.action_id:
                    result.diagnostics.append("UNSUPPORTED_ENCODING")
                continue
            matched = set()
            for _, value in leaves(payload):
                if not isinstance(value, str):
                    continue
                if capture.title_marker in value:
                    matched.add("title")
                if capture.body_marker in value or (text(value) == text(capture.body_marker) and text(value)):
                    matched.add("body")
            if matched:
                result.writes.append({**request, "encoding": encoding, "payload": payload, "roles": matched})
                roles.update(matched)
        if not result.writes:
            result.diagnostics.append("NO_WRITE")
        if roles != {"title", "body"}:
            result.diagnostics.append("CONTENT_NOT_MAPPED")
        if len(result.writes) <= 1:
            result.linked = len(result.writes) == 1 and roles == {"title", "body"}
            return result
        actions = {w.get("action_id") for w in result.writes}
        if len(actions) != 1 or None in actions:
            result.diagnostics.append("UNLINKED_REQUESTS")
            return result
        connected = {result.writes[0].get("id")}
        for earlier, later in zip(result.writes[:-1], result.writes[1:], strict=True):
            response = next((r for r in capture.responses if r.get("request_id") == earlier.get("id")), None)
            if not response or response["status"] >= 400:
                break
            try:
                identity_values = [
                    str(v)
                    for p, v in leaves(json.loads(response["body"]))
                    if p.rsplit(".", 1)[-1] in {"id", "draft_id", "uuid", "topic_id"}
                    and v is not None
                    and not isinstance(v, bool)
                ]
            except ValueError:
                break
            later_values = {
                str(v)
                for p, v in leaves(later["payload"])
                if p.rsplit(".", 1)[-1] in {"id", "draft_id", "uuid", "topic_id"}
            }
            if not any(v in later_values for v in identity_values):
                break
            if response.get("time", 0) > later.get("time", float("inf")):
                break
            connected.add(later.get("id"))
        result.linked = len(connected) == len(result.writes)
        if not result.linked:
            result.diagnostics.append("UNLINKED_REQUESTS")
        return result
