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
        print("\n::error::pytest failed " + report.nodeid.replace("\n", " "), flush=True)
