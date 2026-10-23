"""Value-free HTTP recipes and conservative binding to a new local observation.

Recipes contain types, semantic roles and graph coordinates, never field names,
URLs, selectors, literals, cookies or credentials. Coordinates are hypotheses:
every dependency must match a fresh local response and consuming request.
"""

import json
import re
from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import urlsplit

from bs4 import BeautifulSoup
from pydantic import BaseModel, ConfigDict, Field

from .network import Analysis, Dependency, NetworkAnalyzer, decode, leaves, query_pairs


class Closed(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Operation(Closed):
    method: Literal["POST", "PUT", "PATCH"]
    encoding: Literal["form", "multipart", "json"]
    roles: list[Literal["title", "body"]] = Field(max_length=2)
    shape: list[Literal["object", "array", "string", "number", "boolean", "null"]] = Field(max_length=300)
    route: Literal["draft", "publish", "other"] = "other"


class Binding(Closed):
    operation: int = Field(ge=0, le=19)
    target: int = Field(ge=0, le=299)
    target_kind: Literal["payload", "path", "query", "header"] = "payload"
    parent: int = Field(ge=-1, le=19)  # -1 = local composer GET
    kind: Literal["json_path", "css_attribute", "header", "cookie", "regex"]
    slot: int = Field(ge=0, le=299)
    role: Literal["identity", "fresh"] = "fresh"


class HTTPTemplate(Closed):
    revision: Literal[1] = 1
    operations: list[Operation] = Field(max_length=20)
    bindings: list[Binding] = Field(default_factory=list, max_length=100)
    independent_content_verification: Literal[True] = True


@dataclass
class Transfer:
    analysis: Analysis
    bindings: dict = field(default_factory=dict)
    diagnostics: list[str] = field(default_factory=list)
    reused_operations: int = 0
    reused_dependencies: int = 0


def route_purpose(url):
    parsed = urlsplit(url)
    if re.search(r"(?:^|[/_-])drafts?(?:[/_.-]|$)", parsed.path, re.I):
        return "draft"
    if re.search(
        r"publish|newthread|posting\.php|/(?:discussions|topics|threads|posts)(?:/|$)", parsed.path, re.I
    ):
        return "publish"
    if any(k == "action" and v in {"post", "post2"} for k, v in query_pairs(parsed.query)):
        return "publish"
    return "other"


def shape(value):
    if isinstance(value, dict):
        return ["object", *[x for v in value.values() for x in shape(v)]]
    if isinstance(value, list):
        return ["array", *[x for v in value for x in shape(v)]]
    return [
        "null"
        if value is None
        else "boolean"
        if isinstance(value, bool)
        else "number"
        if isinstance(value, (int, float))
        else "string"
    ]


def source_slots(response, kind):
    if kind == "regex":
        from .javascript import token_slots

        return token_slots(response.get("body", ""))
    if kind == "json_path":
        try:
            return list(leaves(json.loads(response.get("body", ""))))
        except (ValueError, TypeError):
            return []
    if kind == "css_attribute":
        soup = BeautifulSoup(response.get("body", ""), "html.parser")
        return [
            (f"{n.name}[name={json.dumps(n['name'])}]", n.get("value", n.get("content", "")))
            for n in soup.select('input[type="hidden"][name],meta[name][content]')
        ]
    if kind == "header":
        return [
            (k.lower(), v)
            for k, v in response.get("headers", {}).items()
            if k.lower() in {"x-csrf-token", "x-xsrf-token", "x-draft-id", "etag"}
        ]
    return []


def composer(capture):
    target = urlsplit(capture.start_url)._replace(fragment="").geturl()
    return next((r for r in reversed(capture.responses) if r["method"] == "GET" and r["url"] == target), None)


def target_slots(request, kind):
    if kind == "payload":
        return list(leaves(request["payload"]))
    if kind == "query":
        return query_pairs(urlsplit(request["url"]).query)
    if kind == "path":
        from urllib.parse import unquote

        return [(str(i), unquote(v)) for i, v in enumerate(urlsplit(request["url"]).path.split("/"))]
    return [
        (k, v) for k, v in request.get("headers", {}).items() if k.lower() in {"x-csrf-token", "x-xsrf-token"}
    ]


def compile_template(capture, workflow, known_templates=()):
    """Compile only after independent HTTP success; caller owns that proof."""
    analysis = NetworkAnalyzer().analyze(capture)
    if not analysis.linked or analysis.diagnostics:
        for template in known_templates:
            rebound = instantiate(template, capture)
            if not rebound.diagnostics and rebound.analysis.linked:
                analysis = rebound.analysis
                break
    if not analysis.linked or analysis.diagnostics:
        return None
    operations = [
        Operation(
            method=w["method"],
            encoding=w["encoding"],
            roles=sorted(w["roles"]),
            shape=shape(w["payload"]),
            route=route_purpose(w["url"]),
        )
        for w in analysis.writes
    ]
    variables = {}
    bindings = []
    steps = workflow["steps"]
    write_steps = [s for s in steps if s["op"] == "request" and s.get("method", "GET") != "GET"]
    if len(write_steps) != len(operations):
        return None
    sources = {"page": (-1, composer(capture))}
    for i, w in enumerate(analysis.writes):
        sources[write_steps[i].get("save_as")] = (
            i,
            next((r for r in capture.responses if r.get("request_id") == w["id"]), None),
        )
    for s in steps:
        if s["op"] != "extract" or s.get("source") not in sources:
            continue
        parent, response = sources[s["source"]]
        if not response:
            continue
        kind = s["kind"]
        candidates = source_slots(response, kind)
        if kind == "css_attribute":
            selected = BeautifulSoup(response["body"], "html.parser").select(s["selector"])
            matches = [
                j
                for j, (selector, _) in enumerate(candidates)
                if len(selected) == 1
                and BeautifulSoup(response["body"], "html.parser").select_one(selector) == selected[0]
            ]
        else:
            matches = [
                j
                for j, (path, _) in enumerate(candidates)
                if path == s.get("path", s.get("attribute", s.get("pattern")))
            ]
        if len(matches) == 1:
            value = candidates[matches[0]][1]
            if parent == -1 and (not isinstance(value, str) or len(value) < (8 if kind == "regex" else 16)):
                continue
            variables[s["save_as"]] = (parent, kind, matches[0])
    for i, s in enumerate(write_steps):
        payload = s.get(operations[i].encoding, {})
        for j, (_, value) in enumerate(leaves(payload)):
            if not isinstance(value, str):
                continue
            match = re.fullmatch(r"\{\{([a-zA-Z0-9_]+)\}\}", value)
            if match and match[1] in variables:
                parent, kind, slot = variables[match[1]]
                bindings.append(
                    Binding(
                        operation=i,
                        target=j,
                        parent=parent,
                        kind=kind,
                        slot=slot,
                        role="identity" if parent >= 0 else "fresh",
                    )
                )
    indices = {w["id"]: i for i, w in enumerate(analysis.writes)}
    for edge in analysis.dependencies:
        parent, child = indices[edge.parent], indices[edge.child]
        response = sources[write_steps[parent]["save_as"]][1]
        if not response:
            continue
        source = [
            i for i, (p, _) in enumerate(source_slots(response, edge.source_kind)) if p == edge.source_path
        ]
        target = [
            i
            for i, (p, _) in enumerate(target_slots(analysis.writes[child], edge.target_kind))
            if p == edge.target_path
        ]
        if len(source) == len(target) == 1 and not any(
            b.operation == child and b.target_kind == edge.target_kind and b.target == target[0]
            for b in bindings
        ):
            bindings.append(
                Binding(
                    operation=child,
                    target=target[0],
                    target_kind=edge.target_kind,
                    parent=parent,
                    kind=edge.source_kind,
                    slot=source[0],
                    role="identity",
                )
            )
    return HTTPTemplate(operations=operations, bindings=bindings)


def instantiate(template, capture):
    """Map the whole operation graph, then bind only independently observed values.

    No network I/O occurs here. Even a perfect match is an unverified candidate.
    An opaque renamed identity is usable only with both a previously verified
    graph coordinate and an equal, unique, nontrivial value in the local trace.
    """
    raw = NetworkAnalyzer().analyze(capture)
    result = Transfer(raw)
    writes = []
    for request in capture.requests:
        if request.get("blocked") or request.get("action_id") != capture.action_id:
            continue
        if request["method"] not in {"POST", "PUT", "PATCH"}:
            continue
        try:
            encoding, payload = decode(request)
        except (ValueError, LookupError, UnicodeError):
            result.diagnostics.append("TRANSFER_INCOMPATIBLE")
            return result
        roles = set()
        for _, value in leaves(payload):
            if isinstance(value, str):
                import html

                if capture.title_marker in html.unescape(value):
                    roles.add("title")
                if capture.body_marker in html.unescape(value):
                    roles.add("body")
        writes.append({**request, "payload": payload, "encoding": encoding, "roles": roles})
    if len(writes) != len(template.operations):
        result.diagnostics.append("TRANSFER_INCOMPATIBLE")
        return result
    for expected, local in zip(template.operations, writes, strict=True):
        if (
            expected.method != local["method"]
            or expected.encoding != local["encoding"]
            or expected.roles != sorted(local["roles"])
            or expected.shape != shape(local["payload"])
        ):
            result.diagnostics.append("TRANSFER_INCOMPATIBLE")
            return result
    dependencies = list(raw.dependencies)
    for b in template.bindings:
        if b.operation >= len(writes) or b.parent >= b.operation:
            result.diagnostics.append("TRANSFER_INCOMPATIBLE")
            return result
        response = (
            composer(capture)
            if b.parent == -1
            else next((r for r in capture.responses if r.get("request_id") == writes[b.parent]["id"]), None)
        )
        if not response or response.get("status", 500) >= 400:
            result.diagnostics.append("TRANSFER_SOURCE_MISSING")
            return result
        slots = source_slots(response, b.kind)
        targets = target_slots(writes[b.operation], b.target_kind)
        if b.slot >= len(slots) or b.target >= len(targets):
            result.diagnostics.append("TRANSFER_SOURCE_MISSING")
            return result
        path, used = targets[b.target]
        # Reordered/renamed native token fields bind by unique fresh local value.
        if b.parent == -1 and len(str(slots[b.slot][1])) >= 16:
            fresh = [(p, v) for p, v in targets if str(v) == str(slots[b.slot][1])]
            if len(fresh) == 1:
                path, used = fresh[0]
        # Resolve reordering by equality in this installation, never by copying A.
        matches = [(p, v) for p, v in slots if str(v) == str(used) and str(v)]
        if len(matches) != 1 or response.get("time", 0) > writes[b.operation].get("time", float("inf")):
            result.diagnostics.append("TRANSFER_SOURCE_MISSING")
            return result
        source_path, value = matches[0]
        if b.parent >= 0:
            # Short/common values cannot establish a new, otherwise unproven edge.
            existing = any(
                e.parent == writes[b.parent]["id"] and e.child == writes[b.operation]["id"]
                for e in raw.dependencies
            )
            if not existing and (not isinstance(value, str) or len(value) < 16):
                result.diagnostics.append("TRANSFER_AMBIGUOUS")
                return result
            edge = Dependency(
                writes[b.parent]["id"], writes[b.operation]["id"], b.kind, source_path, b.target_kind, path
            )
            if edge not in dependencies:
                dependencies.append(edge)
        if b.target_kind == "payload":
            result.bindings[(writes[b.operation]["id"], path)] = (response, b.kind, source_path)
    result.analysis = Analysis(writes=writes, dependencies=dependencies)
    # Require one connected component containing both content roles, as in normal learning.
    reached = {writes[0]["id"]} if writes else set()
    for _ in writes:
        reached |= {e.child for e in dependencies if e.parent in reached}
        reached |= {e.parent for e in dependencies if e.child in reached}
    result.analysis.linked = len(reached) == len(writes) and set().union(*(w["roles"] for w in writes)) == {
        "title",
        "body",
    }
    if not result.analysis.linked:
        result.diagnostics.append("TRANSFER_AMBIGUOUS")
    else:
        result.reused_operations = len(writes)
        result.reused_dependencies = len(template.bindings)
    return result


def transition_allowed(templates, capture, request, admitted):
    """A prior graph can explain an opaque next write, never an unrelated first write."""
    prior = [r for r in capture.requests if r.get("id") in admitted]
    if not prior:
        return False
    try:
        encoding, payload = decode(
            {"content_type": request.headers.get("content-type", ""), "body": request.post_data_buffer or b""}
        )
    except (ValueError, LookupError, UnicodeError):
        return False
    local = {"payload": payload, "url": request.url, "headers": request.headers}
    for template in templates:
        index = len(prior)
        if index >= len(template.operations):
            continue
        operation = template.operations[index]
        if (
            operation.method != request.method
            or operation.encoding != encoding
            or operation.shape != shape(payload)
            or operation.route == "other"
            or route_purpose(request.url) != operation.route
        ):
            continue
        for b in template.bindings:
            if b.operation != index or not 0 <= b.parent < index:
                continue
            response = next(
                (
                    r
                    for r in capture.responses
                    if r.get("request_id") == prior[b.parent]["id"]
                    and r.get("action_id") == capture.action_id
                    and r.get("status", 500) < 400
                ),
                None,
            )
            if not response:
                continue
            sources, targets = source_slots(response, b.kind), target_slots(local, b.target_kind)
            if b.slot >= len(sources) or b.target >= len(targets):
                continue
            value, used = sources[b.slot][1], targets[b.target][1]
            if isinstance(value, str) and len(value) >= 16 and str(value) == str(used):
                return True
    return False


async def preflight(page, form, template):
    """Check a native form against a recipe without sending any mutation.

    SPA routes and arbitrary JS payloads require a local observation; no guesses.
    Local parameter values remain in the browser and are not returned to the KB.
    """
    if len(template.operations) != 1 or template.operations[0].encoding not in {"form", "multipart"}:
        return {"applicable": False, "reason": "local-network-observation-required", "writes": 0}
    ref = form.fields["body"]
    local = (
        await page.frames[ref.frame]
        .locator(ref.selector)
        .evaluate(
            """e=>{const f=e.form||e.ownerDocument.defaultView.frameElement?.closest('form');
        return f?{method:(f.getAttribute('method')||'get').toUpperCase(),action:new URL(f.getAttribute('action')||location.href,f.baseURI).href,
        fields:[...f.elements].filter(x=>x.name&&!x.disabled).length}:null}"""
        )
    )
    if not local or local["method"] != template.operations[0].method:
        return {"applicable": False, "reason": "form-method-mismatch", "writes": 0}
    from .policy import origin, dangerous_url

    safe = origin(local["action"]) == origin(page.url) and not dangerous_url(local["action"])
    return {
        "applicable": safe,
        "reason": "local-form-bound" if safe else "unsafe-action",
        "operations": len(template.operations),
        "fresh_bindings": len(template.bindings),
        "writes": 0,
        "http_verified": False,
    }
