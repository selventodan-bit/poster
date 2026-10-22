"""Conservative automatic builder for ordinary URL-encoded HTML forms."""

import json
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl, quote, urlsplit, urlunsplit

import yaml
from bs4 import BeautifulSoup

from ..auth import encrypt_session
from ..models import Site, Workflow
from ..project import mark_ready
from .capture import Capture

SENSITIVE = re.compile(
    r"csrf|token|nonce|password|passwd|pwd|secret|auth|api[_-]?key|credential|^sid$|^session_?id$",
    re.IGNORECASE,
)


@dataclass
class Build:
    site: dict
    workflow: dict
    diagnostics: list[str]
    session: dict


def analyze(
    capture: Capture,
    project_id: str,
    confirmation_selector: str | None = None,
    previous: Capture | None = None,
    existing_site: Site | None = None,
) -> Build:
    url = urlsplit(capture.start_url)
    if url.username or url.password:
        raise ValueError("Credential-bearing URLs are unsupported")
    base = f"{url.scheme}://{url.netloc}"
    problems = []

    def declarative_url(value):
        parsed = urlsplit(value)
        pairs = []
        for key, item in parse_qsl(parsed.query, keep_blank_values=True):
            if SENSITIVE.search(key):
                problems.append("Sensitive query parameter needs an explicit dynamic source")
                item = "{{operator_review_required}}"
            else:
                item = quote(item, safe="")
            pairs.append(quote(key, safe="") + "=" + item)
        return urlunsplit((parsed.scheme, parsed.netloc.rsplit("@", 1)[-1], parsed.path, "&".join(pairs), ""))

    candidates = [
        r
        for r in capture.requests
        if r["method"] in {"POST", "PUT", "PATCH"}
        and capture.title_marker in r["body"]
        and capture.body_marker in r["body"]
    ]
    site = {
        "schema_version": 1,
        "id": project_id,
        "base_url": base,
        "allowed_hosts": [url.hostname],
        "mode": "http",
        "enabled": True,
        "status": "REVIEW",
        "workflow": "workflow.yaml",
    }
    if existing_site:
        preserved = existing_site.model_dump(
            exclude={"auth_file", "workflow", "status", "id", "base_url", "allowed_hosts"}
        )
        site.update(preserved)
    workflow = {"schema_version": 1, "steps": []}
    if len(candidates) != 1:
        return Build(
            {**site, "status": "BROWSER_REQUIRED"},
            workflow,
            ["Expected one write request containing both markers"],
            capture.session,
        )
    submit = candidates[0]
    if "application/x-www-form-urlencoded" not in submit["content_type"]:
        return Build(site, workflow, ["Automatic builder supports URL-encoded forms only"], capture.session)
    if urlsplit(submit["url"]).netloc != url.netloc:
        problems.append("Cross-origin submission needs manual review")
    fields = dict(parse_qsl(submit["body"], keep_blank_values=True))
    if len(fields) != len(parse_qsl(submit["body"], keep_blank_values=True)):
        problems.append("Repeated form field names need manual review")
    forms = [f for f in capture.forms if f.get("action") == submit["url"]]
    form = forms[-1] if forms else None
    if not form:
        problems.append("No DOM form snapshot matched the submit request")
    steps = [
        {
            "id": "open_form",
            "op": "request",
            "url": declarative_url(form["url"] if form else capture.start_url),
            "save_as": "page",
        }
    ]
    title_found = body_found = False
    for index, (name, value) in enumerate(fields.items()):
        if capture.title_marker in value:
            fields[name] = value.replace(capture.title_marker, "{{title}}")
            title_found = True
        elif capture.body_marker in value:
            fields[name] = value.replace(capture.body_marker, "{{body}}")
            body_found = True
        else:
            node = next((f for f in (form or {}).get("fields", []) if f["name"] == name), None)
            if node and node["type"] == "password":
                fields[name] = "{{operator_review_required}}"
                problems.append("Password input is never exported as plaintext")
            elif node and node["type"] == "hidden":
                variable = f"hidden_{index}"
                steps.append(
                    {
                        "id": f"extract_{index}",
                        "op": "extract",
                        "source": "page",
                        "kind": "css_attribute",
                        "selector": node["selector"],
                        "attribute": "value",
                        "save_as": variable,
                    }
                )
                fields[name] = "{{" + variable + "}}"
            elif SENSITIVE.search(name):
                fields[name] = "{{operator_review_required}}"
                problems.append("Sensitive/dynamic field has no established DOM source")
    if not title_found or not body_found:
        problems.append("Could not map title and body")
    if any(f["type"] in {"password", "file"} for f in (form or {}).get("fields", [])):
        problems.append("Login/file form is not supported by automatic builder")
    # Reject JS-generated hidden values that are not present in the preceding GET HTML.
    source_pages = [
        r for r in capture.responses if r["url"] == (form or {}).get("url") and r["method"] == "GET"
    ]
    if any(s["op"] == "extract" for s in steps):
        soup = BeautifulSoup(source_pages[-1]["body"] if source_pages else "", "html.parser")
        if any(s["op"] == "extract" and not soup.select_one(s["selector"]) for s in steps):
            problems.append("Hidden values originate in JavaScript, not GET HTML")
        raw_fields = dict(parse_qsl(submit["body"], keep_blank_values=True))
        for hidden in (form or {}).get("fields", []):
            if hidden["type"] == "hidden" and hidden["name"] in raw_fields:
                node = soup.select_one(hidden["selector"])
                if node is not None and node.get("value") != raw_fields[hidden["name"]]:
                    problems.append("Hidden value changed after GET; JavaScript transform requires review")
    if previous:
        older = [
            r for r in previous.requests if r["method"] == submit["method"] and r["url"] == submit["url"]
        ]
        if older:
            old = dict(parse_qsl(older[-1]["body"], keep_blank_values=True))
            for name, value in fields.items():
                if "{{" not in value and old.get(name) != value:
                    problems.append("Unexplained changing field across two captures")
    steps.append(
        {
            "id": "submit",
            "op": "request",
            "method": submit["method"],
            "url": declarative_url(submit["url"]),
            "form": fields,
            "save_as": "submitted",
        }
    )
    pages = [r for r in capture.responses if r["status"] == 200]
    final_html = pages[-1]["body"] if pages else ""
    selector = confirmation_selector
    if not selector and BeautifulSoup(final_html, "html.parser").select_one("[data-result-id]"):
        selector = "[data-result-id]"
    if selector:
        if not BeautifulSoup(final_html, "html.parser").select_one(selector):
            problems.append("Confirmation selector did not match the recorded result")
        steps.append(
            {
                "id": "confirmation",
                "op": "verify",
                "source": "submitted",
                "kind": "css_text",
                "selector": selector,
                "result_url": True,
            }
        )
        # For fixture/data-result-id pages, verify the actual title and body too.
        if selector == "[data-result-id]":
            steps.extend(
                [
                    {
                        "id": "verify_title",
                        "op": "verify",
                        "source": "submitted",
                        "kind": "css_text",
                        "selector": f"{selector} h1",
                        "expected": "{{title}}",
                    },
                    {
                        "id": "verify_body",
                        "op": "verify",
                        "source": "submitted",
                        "kind": "css_text",
                        "selector": f"{selector} .body",
                        "expected": "{{body}}",
                    },
                ]
            )
    else:
        problems.append("No semantic success selector; supply --confirm-selector and reanalyze")
    workflow["steps"] = steps
    # Keep REVIEW projects exportable, but do not imply they can enter the executable registry.
    site["status"] = "REVIEW" if problems else "HTTP_READY"
    Site.model_validate(site)
    if not problems:
        Workflow.model_validate(workflow)
    return Build(site, workflow, sorted(set(problems)), capture.session)


def export(build: Build, output: Path):
    if output.exists() and any(output.iterdir()):
        raise ValueError("Export directory must be empty; publish a new revision separately")
    output.mkdir(parents=True, exist_ok=True)
    site = dict(build.site)
    Site.model_validate(site)
    state = build.session
    if state.get("cookies") or state.get("origins") or state.get("headers"):
        encrypted = encrypt_session(state)  # fail before writing if the key is missing
        (output / "session.enc").write_bytes(encrypted)
        site["auth_file"] = "session.enc"
    (output / "site.yaml").write_text(yaml.safe_dump(site, sort_keys=False), encoding="utf-8")
    (output / "workflow.yaml").write_text(yaml.safe_dump(build.workflow, sort_keys=False), encoding="utf-8")
    (output / "analysis.json").write_text(
        json.dumps({"status": site["status"], "diagnostics": build.diagnostics}, indent=2), encoding="utf-8"
    )
    mark_ready(output)
