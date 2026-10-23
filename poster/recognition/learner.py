"""Build schema-v1 candidates only from observed payloads, dynamic sources and result evidence."""

import html
import json
import re
from urllib.parse import quote, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from ..models import Site, Workflow
from ..recorder.builder import Build, SENSITIVE
from .network import NetworkAnalyzer, leaves, text, query_pairs
from .policy import origin as url_origin
from .transfer import instantiate
from .javascript import token_slots


class HTTPWorkflowLearner:
    def learn(self, capture, project_id, *, rule_id=None, existing_site=None, knowledge_rule=None):
        analysis = NetworkAnalyzer().analyze(capture)
        transfer = None
        if knowledge_rule:
            matches = [instantiate(t, capture) for t in knowledge_rule.http_templates]
            matches = [m for m in matches if not m.diagnostics and m.analysis.linked]
            if matches:
                transfer = max(matches, key=lambda m: m.reused_dependencies)
                analysis = transfer.analysis
        self.transfer_report = {
            "applied": transfer is not None,
            "operations": transfer.reused_operations if transfer else 0,
            "bindings": transfer.reused_dependencies if transfer else 0,
        }
        if knowledge_rule and knowledge_rule.http_templates and not transfer:
            self.transfer_report["declined"] = sorted(
                {
                    code
                    for template in knowledge_rule.http_templates
                    for code in instantiate(template, capture).diagnostics
                }
            )
        diagnostics = list(analysis.diagnostics)
        if not capture.browser_verified or not capture.confirmation:
            diagnostics.append("NO_PERMALINK")
        origin = urlsplit(capture.start_url)
        site = {
            "schema_version": 1,
            "id": project_id,
            "base_url": f"{origin.scheme}://{origin.netloc}",
            "allowed_hosts": [origin.hostname],
            "workflow": "workflow.yaml",
            "status": "REVIEW",
            "mode": "http",
        }
        if existing_site:
            site.update(
                existing_site.model_dump(
                    exclude={
                        "id",
                        "base_url",
                        "allowed_hosts",
                        "status",
                        "recognition",
                        "auth_file",
                        "workflow",
                    }
                )
            )
        steps, sources, bindings = [], {}, {}
        preferred_sources = set(p.source for p in knowledge_rule.tokens.patterns) if knowledge_rule else set()

        def source_rank(response):
            kind = "json"
            if "<meta" in response.get("body", ""):
                kind = "html-meta"
            elif "<input" in response.get("body", ""):
                kind = "html-hidden"
            if response.get("headers", {}).get("x-csrf-token"):
                kind = "header"
            return (
                urlsplit(response["url"])._replace(fragment="").geturl()
                != urlsplit(capture.start_url)._replace(fragment="").geturl(),
                kind not in preferred_sources,
                -response.get("time", 0),
            )

        evidence_responses = sorted(capture.responses, key=source_rank)

        def cookie_name(value):
            return next(
                (
                    c["name"]
                    for c in capture.session.get("cookies", [])
                    if c["value"] == value and re.fullmatch(r"[A-Za-z0-9_\-]{1,100}", c["name"])
                ),
                None,
            )

        def endpoint(raw, request=None):
            parsed = urlsplit(raw)
            if url_origin(raw) != url_origin(capture.start_url) or parsed.username or parsed.password:
                diagnostics.append("BLOCKED_HOST")
                return "{{operator_review_required}}"
            query = []
            for k, v in query_pairs(parsed.query):
                edge = next(
                    (
                        e
                        for e in analysis.dependencies
                        if request
                        and e.child == request.get("id")
                        and e.target_kind == "query"
                        and e.target_path == k
                    ),
                    None,
                )
                if edge:
                    v = dependency_binding(edge).replace("}}", "|url}}")
                elif SENSITIVE.search(k) and cookie_name(v):
                    # Authentication is supplied by the encrypted cookie jar. Never
                    # duplicate the captured session value in a persistent URL.
                    if request is None:
                        continue
                    v = extraction("page", "cookie", attribute=cookie_name(v)).replace("}}", "|url}}")
                elif SENSITIVE.search(k):
                    diagnostics.append("UNKNOWN_DYNAMIC")
                    v = "{{operator_review_required}}"
                else:
                    v = quote(v, safe="")
                query.append(quote(k, safe="") + "=" + v)
            segments = parsed.path.split("/")
            for edge in analysis.dependencies:
                if request and edge.child == request.get("id") and edge.target_kind == "path":
                    segments[int(edge.target_path)] = dependency_binding(edge).replace("}}", "|url}}")
            if any(re.fullmatch(r"[a-f0-9-]{24,}", part, re.I) for part in segments):
                diagnostics.append("UNKNOWN_DYNAMIC")
                segments = [
                    "{{operator_review_required}}" if re.fullmatch(r"[a-f0-9-]{24,}", part, re.I) else part
                    for part in segments
                ]
            return urlunsplit((parsed.scheme, parsed.netloc, "/".join(segments), "&".join(query), ""))

        initial = next(
            (
                r
                for r in reversed(capture.responses)
                if r["method"] == "GET"
                and r["url"] == urlsplit(capture.start_url)._replace(fragment="").geturl()
            ),
            None,
        )
        steps.append(
            {"id": "open_form", "op": "request", "url": endpoint(capture.start_url), "save_as": "page"}
        )
        if initial:
            sources[initial.get("request_id")] = "page"

        def bind(value, key, request, full_path=None):
            if value == "":
                return value
            # Template paths are resolved against current local payload names;
            # a prior installation never supplies values, names, URLs or tokens.
            local = [
                (path, binding)
                for (request_id, path), binding in (transfer.bindings.items() if transfer else [])
                if request_id == request.get("id")
                and (path == full_path if full_path is not None else path.rsplit(".", 1)[-1] == key)
            ]
            if len(local) == 1:
                response, kind, path = local[0][1]
                source = ensure_source(response)
                if source:
                    if kind == "css_attribute":
                        node = BeautifulSoup(response["body"], "html.parser").select_one(path)
                        return extraction(
                            source,
                            kind,
                            selector=path,
                            attribute="value" if node.name == "input" else "content",
                        )
                    if kind == "regex":
                        return extraction(source, kind, pattern=path)
                    return extraction(
                        source, kind, **({"path": path} if kind == "json_path" else {"attribute": path})
                    )
            edge = next(
                (
                    e
                    for e in analysis.dependencies
                    if e.child == request.get("id")
                    and e.target_path.rsplit(".", 1)[-1] == key
                    and e.target_kind in {"payload", "header"}
                ),
                None,
            )
            if edge:
                return dependency_binding(edge)
            if not isinstance(value, str):
                if (
                    isinstance(value, (int, float))
                    and not isinstance(value, bool)
                    and re.search(r"(^id$|_id$)", key)
                ):
                    for response in capture.responses:
                        source = sources.get(response.get("request_id"))
                        if not source or response.get("time", 0) > request.get("time", float("inf")):
                            continue
                        try:
                            payload = json.loads(response["body"])
                        except ValueError:
                            continue
                        for path, previous in leaves(payload):
                            if previous == value and path.rsplit(".", 1)[-1] in {
                                "id",
                                "draft_id",
                                "topic_id",
                            }:
                                return extraction(source, "json_path", path=path)
                if re.search(r"(^id$|_id$)", key) and key not in {"forum_id", "category_id"}:
                    diagnostics.append("UNKNOWN_DYNAMIC")
                    return "{{operator_review_required}}"
                return value
            # Rich text is templated without losing escaping of the next task's body.
            body = capture.body_marker
            if capture.body_format == "text" and value.replace("\r\n", "\n") == body.replace("\r\n", "\n"):
                return "{{body}}"
            escaped_title = html.escape(capture.title_marker, quote=False)
            if escaped_title != capture.title_marker and escaped_title in value:
                return value.replace(escaped_title, "{{title|html}}")
            escaped_body = html.escape(body, quote=False)
            escaped_lines = escaped_body.replace("\r\n", "\n").replace("\n", "<br>")
            if escaped_lines != escaped_body and escaped_lines in value:
                return value.replace(escaped_lines, "{{body|html_lines}}")
            if (
                escaped_body
                and escaped_body in value
                and (
                    capture.body_format == "html"
                    or escaped_body != body
                    or BeautifulSoup(value, "html.parser").find()
                )
            ):
                return value.replace(escaped_body, "{{body|html}}")
            if body in value:
                return (
                    value.replace(body, "{{body}}").replace(capture.title_marker, "{{title}}")
                    if "title" in capture.content_roles
                    else value.replace(body, "{{body}}")
                )
            if "title" in capture.content_roles and capture.title_marker in value:
                return value.replace(capture.title_marker, "{{title}}")
            if text(value) == text(body) and text(body):
                diagnostics.append("CONTENT_NOT_MAPPED")
                return "{{operator_review_required}}"
            sensitive = bool(SENSITIVE.search(key))
            cookie = cookie_name(value)
            if cookie and sensitive and value:
                return extraction("page", "cookie", attribute=cookie)
            # Sources must precede the consuming write, including both GET and earlier JSON responses.
            for response in evidence_responses:
                if response.get("time", 0) > request.get("time", float("inf")):
                    continue
                for header, previous in response.get("headers", {}).items():
                    if (
                        header.lower() in {"x-csrf-token", "x-xsrf-token", "x-draft-id", "etag"}
                        and str(previous) == value
                        and sensitive
                    ):
                        source = ensure_source(response)
                        if source:
                            return extraction(source, "header", attribute=header.lower())
                if response["method"] == "GET":
                    soup = BeautifulSoup(response["body"], "html.parser")
                    node = next(
                        (
                            n
                            for n in soup.select('input[type="hidden"][name],meta[name][content]')
                            if n.get("value", n.get("content")) == value
                            and (n.get("name") == key or sensitive)
                        ),
                        None,
                    )
                    if node:
                        source_key = response.get("request_id")
                        if source_key not in sources:
                            source_name = f"source_{len(sources)}"
                            steps.append(
                                {
                                    "id": source_name,
                                    "op": "request",
                                    "url": endpoint(response["url"]),
                                    "save_as": source_name,
                                }
                            )
                            sources[source_key] = source_name
                        source_name = sources[source_key]
                        selector = f"{node.name}[name={json.dumps(node['name'])}]"
                        if len(soup.select(selector)) > 1:
                            # Header search/login forms can share hidden names
                            # with the composer (notably MyBB's "action").
                            # A structural CSS path stays local to this project.
                            parts, current = [], node
                            while current and current.name not in {"html", "[document]"}:
                                siblings = current.parent.find_all(current.name, recursive=False)
                                parts.append(current.name + f":nth-of-type({siblings.index(current) + 1})")
                                current = current.parent
                            selector = " > ".join(reversed(parts))
                        attribute = "value" if node.name == "input" else "content"
                        return extraction(
                            source_name, "css_attribute", selector=selector, attribute=attribute
                        )
                if response["method"] == "GET" and sensitive:
                    matches = [
                        (pattern, candidate)
                        for pattern, candidate in token_slots(response["body"])
                        if candidate == value
                    ]
                    if len(matches) == 1:
                        source = ensure_source(response)
                        if source:
                            return extraction(source, "regex", pattern=matches[0][0])
                try:
                    payload = json.loads(response["body"])
                except ValueError:
                    continue
                for path, candidate in leaves(payload):
                    if str(candidate) != value or not value:
                        continue
                    role = path.rsplit(".", 1)[-1]
                    if role != key and not (sensitive or role in {"id", "uuid", "draft_id", "topic_id"}):
                        continue
                    response_id = response.get("request_id")
                    if response_id not in sources:
                        if response["method"] != "GET":
                            continue
                        source_name = f"source_{len(sources)}"
                        steps.append(
                            {
                                "id": source_name,
                                "op": "request",
                                "url": endpoint(response["url"]),
                                "save_as": source_name,
                            }
                        )
                        sources[response_id] = source_name
                    return extraction(sources[response_id], "json_path", path=path)
            if sensitive or (
                re.search(r"(^id$|_id$|nonce|hash|checksum)", key, re.I)
                and key not in {"forum_id", "category_id"}
            ):
                diagnostics.append("UNKNOWN_DYNAMIC")
                return "{{operator_review_required}}"
            if re.fullmatch(r"[A-Za-z0-9_+/=-]{24,}", value):
                diagnostics.append("UNKNOWN_DYNAMIC")
                return "{{operator_review_required}}"
            return value

        def extraction(source, kind, **kwargs):
            identity = json.dumps([source, kind, kwargs], sort_keys=True)
            if identity not in bindings:
                var = f"dynamic_{len(bindings)}"
                steps.append(
                    {
                        "id": "extract_" + var,
                        "op": "extract",
                        "source": source,
                        "kind": kind,
                        "save_as": var,
                        **kwargs,
                    }
                )
                bindings[identity] = var
            return "{{" + bindings[identity] + "}}"

        def ensure_source(response):
            response_id = response.get("request_id")
            if response_id not in sources:
                if response["method"] != "GET":
                    return None
                name = f"source_{len(sources)}"
                steps.append({"id": name, "op": "request", "url": endpoint(response["url"]), "save_as": name})
                sources[response_id] = name
            return sources[response_id]

        def dependency_binding(edge):
            source = sources.get(edge.parent)
            if not source:
                diagnostics.append("UNKNOWN_DYNAMIC")
                return "{{operator_review_required}}"
            kwargs = (
                {"path": edge.source_path}
                if edge.source_kind == "json_path"
                else {"attribute": edge.source_path}
            )
            return extraction(source, edge.source_kind, **kwargs)

        def mapping(payload, request, key="", path=""):
            if isinstance(payload, dict):
                return {k: mapping(v, request, k, f"{path}.{k}".strip(".")) for k, v in payload.items()}
            if isinstance(payload, list):
                return [mapping(v, request, key, f"{path}.{i}".strip(".")) for i, v in enumerate(payload)]
            return bind(payload, key, request, path)

        if analysis.linked:
            if initial and BeautifulSoup(initial["body"], "html.parser").select_one(
                'input[name="creation_time"]'
            ):
                steps.append({"id": "respect_form_time", "op": "wait", "seconds": 1.1})
            for i, request in enumerate(analysis.writes):
                payload = mapping(request["payload"], request)
                headers = {
                    k: bind(v, k, request)
                    for k, v in request.get("headers", {}).items()
                    if k.lower() in {"x-csrf-token", "x-xsrf-token"}
                }
                name = f"write_{i}"
                steps.append(
                    {
                        "id": name,
                        "op": "request",
                        "url": endpoint(request["url"], request),
                        "method": request["method"],
                        request["encoding"]: payload,
                        "headers": headers,
                        "save_as": name,
                    }
                )
                sources[request.get("id")] = name
            confirmation = capture.confirmation
            if confirmation:
                source = f"write_{len(analysis.writes) - 1}"
                if confirmation.get("identity_path") and confirmation.get("result_prefix"):
                    prefix = confirmation["result_prefix"]
                    if not prefix.startswith("/") or prefix.startswith("//") or "?" in prefix:
                        diagnostics.append("BLOCKED_HOST")
                    else:
                        steps.extend(
                            [
                                {
                                    "id": "result_identity",
                                    "op": "extract",
                                    "source": source,
                                    "kind": "json_path",
                                    "path": confirmation["identity_path"],
                                    "save_as": "result_id",
                                },
                                {
                                    "id": "read_result",
                                    "op": "request",
                                    "url": prefix + "{{result_id|url}}",
                                    "save_as": "publication",
                                },
                            ]
                        )
                        source = "publication"
                elif confirmation.get("url_path"):
                    steps.append(
                        {
                            "id": "result_link",
                            "op": "extract",
                            "source": source,
                            "kind": "json_path",
                            "path": confirmation["url_path"],
                            "save_as": "result_link",
                        }
                    )
                    steps.append(
                        {
                            "id": "read_result",
                            "op": "request",
                            "url": "{{result_link}}",
                            "save_as": "publication",
                        }
                    )
                    source = "publication"
                elif confirmation.get("link_selector"):
                    steps.append(
                        {
                            "id": "result_link",
                            "op": "extract",
                            "source": source,
                            "kind": "css_attribute",
                            "selector": confirmation["link_selector"],
                            "attribute": "href",
                            "save_as": "result_link",
                        }
                    )
                    steps.append(
                        {
                            "id": "read_result",
                            "op": "request",
                            "url": "{{result_link}}",
                            "save_as": "publication",
                        }
                    )
                    source = "publication"
                for role in sorted(capture.content_roles):
                    steps.append(
                        {
                            "id": "verify_" + role,
                            "op": "verify",
                            "source": source,
                            "kind": "css_text",
                            "selector": confirmation[role + "_selector"],
                            "expected": "{{" + role + "}}",
                            "normalize": "whitespace",
                            "result_url": True,
                        }
                    )
        state = "HTTP_CANDIDATE" if not diagnostics and analysis.linked else "LEARNING_REQUIRED"
        site["recognition"] = {
            "state": state,
            "recognizer": "relational-2.2",
            "publication_kind": capture.publication_kind,
            "rule_id": rule_id,
            "browser_verified": capture.browser_verified,
            "http_verified": False,
            "diagnostics": sorted(set(diagnostics)),
        }
        Site.model_validate(site)
        if state == "HTTP_CANDIDATE":
            Workflow.model_validate({"schema_version": 1, "steps": steps})
        return Build(site, {"schema_version": 1, "steps": steps}, sorted(set(diagnostics)), capture.session)
