import pytest
from cryptography.fernet import Fernet

from poster.fixtures import serve_fixture
from poster.models import Site, Workflow
from poster.project import Project


@pytest.fixture
def fixture_site():
    with serve_fixture() as server:
        yield server


@pytest.fixture(autouse=True)
def session_key(monkeypatch):
    monkeypatch.setenv("POSTER_SESSION_KEY", Fernet.generate_key().decode())


def project_for(base, *, csrf=False, steps=None, recovery=None, **site_options):
    kind = "csrf" if csrf else "classic"
    workflow = [{"id": "open", "op": "request", "url": f"/{kind}/new", "save_as": "page"}]
    form = {"subject": "{{title}}", "message": "{{body}}"}
    if csrf:
        workflow.append(
            {
                "id": "token",
                "op": "extract",
                "source": "page",
                "kind": "css_attribute",
                "selector": "[name=csrf_token]",
                "attribute": "value",
                "save_as": "csrf",
            }
        )
        form["csrf_token"] = "{{csrf}}"
    workflow.extend(
        [
            {
                "id": "send",
                "op": "request",
                "method": "POST",
                "url": f"/{kind}/posts",
                "form": form,
                "save_as": "posted",
            },
            {
                "id": "verified",
                "op": "verify",
                "source": "posted",
                "kind": "css_text",
                "selector": "[data-result-id] h1",
                "expected": "{{title}}",
                "result_url": True,
            },
        ]
    )
    from urllib.parse import urlsplit

    site = Site(
        id="csrf" if csrf else "classic",
        base_url=base,
        allowed_hosts=[urlsplit(base).hostname],
        status="HTTP_READY",
        **site_options,
    )
    return Project(
        site, Workflow.model_validate({"steps": steps or workflow, "recovery": recovery or []}), "revision1"
    )


@pytest.fixture
def make_project():
    return project_for


def pytest_runtest_logreport(report):
    # CI annotations contain only a test identifier; never request bodies/secrets.
    if report.failed:
        crash = getattr(report.longrepr, "reprcrash", None)
        where = f" at line {crash.lineno}" if crash else ""
        print("\n::error::pytest failed " + report.nodeid.replace("\n", " ") + where, flush=True)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    result = yield
    report = result.get_result()
    if not report.failed or not item.path.name.startswith("test_dispatcher"):
        return
    # Hosted Windows logs are not always downloadable. Publish only fixed codes
    # and locations, never inputs, exception text, stdout/stderr or graph values.
    import json
    from pathlib import Path
    import sqlite3
    import traceback

    from poster.dispatcher.models import MESSAGES
    from poster.maker.runtime import ERRORS

    codes = set(MESSAGES) | set(ERRORS)
    diagnostic = {"test": item.name, "frames": [], "executions": []}
    if call.excinfo:
        diagnostic["exception_type"] = type(call.excinfo.value).__name__
        diagnostic["frames"] = [
            Path(frame.filename).name + ":" + str(frame.lineno)
            for frame in traceback.extract_tb(call.excinfo.value.__traceback__)[-6:]
        ]
        code = getattr(call.excinfo.value, "code", None)
        if code in codes:
            diagnostic["code"] = code
    root = item.funcargs.get("tmp_path")
    for path in root.rglob("*.sqlite") if root else ():
        try:
            with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as connection:
                rows = connection.execute("SELECT status,code,exit_code,report FROM executions LIMIT 10")
                for status, code, exit_code, filename in rows:
                    entry = {"status": status, "code": code if code in codes else None, "exit": exit_code}
                    if filename and Path(filename).is_file():
                        safe = json.loads(Path(filename).read_bytes())
                        entry["node_code"] = (
                            safe.get("node_error_code") if safe.get("node_error_code") in codes else None
                        )
                    diagnostic["executions"].append(entry)
        except (OSError, ValueError, sqlite3.Error):
            pass
    print("\n::error title=Dispatcher diagnostics::" + json.dumps(diagnostic), flush=True)
