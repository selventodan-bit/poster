"""Build schema-v1 candidates only from observed payloads, dynamic sources and result evidence."""

import html
import json
import re
from urllib.parse import parse_qsl, quote, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from ..models import Site, Workflow
from ..recorder.builder import Build, SENSITIVE
from .network import NetworkAnalyzer, leaves, text


class HTTPWorkflowLearner:
    def learn(self, capture, project_id, *, rule_id=None, existing_site=None):
        analysis = NetworkAnalyzer().analyze(capture)
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

        def endpoint(raw):
            parsed = urlsplit(raw)
            if parsed.hostname != origin.hostname or parsed.username or parsed.password:
                diagnostics.append("BLOCKED_HOST")
                return "{{operator_review_required}}"
            query = []
            for k, v in parse_qsl(parsed.query, keep_blank_values=True):
                if SENSITIVE.search(k):
                    diagnostics.append("UNKNOWN_DYNAMIC")
                    v = "{{operator_review_required}}"
                else:
                    v = quote(v, safe="")
                query.append(quote(k, safe="") + "=" + v)
            return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "&".join(query), ""))

        initial = next(
            (r for r in capture.responses if r["method"] == "GET" and r["url"] == capture.start_url), None
        )
        steps.append(
            {"id": "open_form", "op": "request", "url": endpoint(capture.start_url), "save_as": "page"}
        )
        if initial:
            sources[initial.get("request_id")] = "page"

        def bind(value, key, request):
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
                return value
            # Rich text is templated without losing escaping of the next task's body.
            body = capture.body_marker
            escaped_body = html.escape(body, quote=False)
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
                return value.replace(body, "{{body}}").replace(capture.title_marker, "{{title}}")
            if capture.title_marker in value:
                return value.replace(capture.title_marker, "{{title}}")
            if text(value) == text(body) and text(body):
                diagnostics.append("CONTENT_NOT_MAPPED")
                return "{{operator_review_required}}"
            sensitive = bool(SENSITIVE.search(key))
            # Sources must precede the consuming write, including both GET and earlier JSON responses.
            for response in capture.responses:
                if response.get("time", 0) > request.get("time", float("inf")):
                    continue
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
                        attribute = "value" if node.name == "input" else "content"
                        return extraction(
                            source_name, "css_attribute", selector=selector, attribute=attribute
                        )
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
                and re.fullmatch(r"[a-fA-F0-9-]{16,}", value)
            ):
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

        def mapping(payload, request, key=""):
            if isinstance(payload, dict):
                return {k: mapping(v, request, k) for k, v in payload.items()}
            if isinstance(payload, list):
                return [mapping(v, request, key) for v in payload]
            return bind(payload, key, request)

        if analysis.linked:
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
                        "url": endpoint(request["url"]),
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
                if confirmation.get("url_path"):
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
                for role in ("title", "body"):
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
            "recognizer": "semantic-2.0",
            "rule_id": rule_id,
            "browser_verified": capture.browser_verified,
            "http_verified": False,
            "diagnostics": sorted(set(diagnostics)),
        }
        Site.model_validate(site)
        if state == "HTTP_CANDIDATE":
            Workflow.model_validate({"schema_version": 1, "steps": steps})
        return Build(site, {"schema_version": 1, "steps": steps}, sorted(set(diagnostics)), capture.session)
