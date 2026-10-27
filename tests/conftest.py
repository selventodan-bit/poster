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
    if not report.failed:
        return
    if item.path.name == "test_user_readiness.py" and call.excinfo:
        # Browser transports can fail before RecognitionSession records a safe
        # diagnostic. Expose only locations and fixed categories, never the
        # Playwright message (it may include page text, credentials or URLs).
        import json
        from pathlib import Path
        import traceback

        exc = call.excinfo.value
        message = str(exc).lower()
        hint = next(
            (
                code
                for phrase, code in (
                    ("execution context was destroyed", "NAVIGATION_CONTEXT_CHANGED"),
                    ("navigation is interrupted", "NAVIGATION_INTERRUPTED"),
                    ("target page, context or browser has been closed", "TARGET_CLOSED"),
                    ("net::err_", "NETWORK_ERROR"),
                    ("timeout", "BROWSER_TIMEOUT"),
                )
                if phrase in message
            ),
            "UNCLASSIFIED",
        )
        diagnostic = dict(
            test=item.name,
            phase=report.when,
            exception_type=type(exc).__name__,
            hint=hint,
            frames=[
                Path(f.filename).name + ":" + str(f.lineno) + ":" + f.name
                for f in traceback.extract_tb(exc.__traceback__)[-12:]
            ],
        )
        print("\n::notice title=Readiness browser diagnostics::" + json.dumps(diagnostic), flush=True)
        return
    if not item.path.name.startswith(
        ("test_dispatcher", "remote_", "test_portability", "portability_", "cloud_browser_instances")
    ):
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
    diagnostic["worker_failures"] = []
    for failure in root.rglob("worker-failure.json") if root else ():
        try:
            diagnostic["worker_failures"].append(json.loads(failure.read_bytes()))
        except (OSError, ValueError):
            pass
    for path in root.rglob("*.sqlite") if root else ():
        try:
            with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as connection:
                rows = connection.execute(
                    "SELECT status,code,exit_code,report,pid,birth,last_confirmed_step,effects_possible FROM executions ORDER BY leased DESC LIMIT 10"
                )
                for status, code, exit_code, filename, pid, birth, step, effects in rows:
                    entry = {"status": status, "code": code if code in codes else None, "exit": exit_code}
                    entry.update(last_confirmed_step=step, effects_possible=bool(effects))
                    if status in {"LEASED", "RUNNING"} and pid:
                        from poster.dispatcher.processes import same_process
                        import psutil

                        process = same_process(pid, birth)
                        entry["processes"] = []
                        if process:
                            try:
                                for child in [process, *process.children(recursive=True)][:12]:
                                    name = Path(child.exe()).stem.lower()
                                    kind = (
                                        "driver"
                                        if name == "node"
                                        else "browser"
                                        if name in {"chrome", "chromium", "msedge"}
                                        else "other"
                                    )
                                    entry["processes"].append(
                                        {
                                            "pid": child.pid,
                                            "kind": kind,
                                            "state": child.status(),
                                            "threads": child.num_threads(),
                                        }
                                    )
                            except psutil.Error:
                                pass
                    if filename and Path(filename).is_file():
                        safe = json.loads(Path(filename).read_bytes())
                        entry["node_code"] = (
                            safe.get("node_error_code") if safe.get("node_error_code") in codes else None
                        )
                    diagnostic["executions"].append(entry)
                diagnostic["events"] = [
                    {"status": state, "code": code if code in codes else None}
                    for state, code in connection.execute(
                        "SELECT status,code FROM events ORDER BY id DESC LIMIT 12"
                    )
                ]
        except (OSError, ValueError, sqlite3.Error):
            pass
    print("\n::error title=Dispatcher diagnostics::" + json.dumps(diagnostic), flush=True)


@pytest.fixture(autouse=True)
def isolated_product_lease_registry(tmp_path, monkeypatch):
    # Real OS locks and PID/birth records, isolated from the developer's own
    # running apps. Child processes inherit this same per-test registry.
    monkeypatch.setenv("POSTER_PRODUCT_LOCK_HOME", str(tmp_path / ".poster-product-locks"))
