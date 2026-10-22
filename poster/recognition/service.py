"""A bounded, consent-gated browser session; the executor stays browser-free."""

import asyncio
import json
import hashlib
import re
import uuid
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup

from ..auth import encrypt_session
from ..models import Site, Step, Workflow
from ..project import Project
from ..recorder.builder import export
from ..recorder.capture import Recorder, chromium_options
from ..runtime import Runtime, WorkflowError
from .detector import EngineDetector
from .forms import UniversalFormDetector
from .learner import HTTPWorkflowLearner
from .network import NetworkAnalyzer
from .network import leaves
from .types import FieldRef, FormCandidate, LearningState, Recognition

OPEN_FORM = re.compile(
    r"new topic|create topic|new thread|new discussion|nuevo tema|crear tema|nova mensagem|"
    r"новая тема|создать тему",
    re.I,
)


def css_path(node):
    parts = []
    while node and node.name not in {"[document]", "html"}:
        siblings = [n for n in node.parent.find_all(node.name, recursive=False)] if node.parent else [node]
        parts.append(node.name + (f":nth-of-type({siblings.index(node) + 1})" if len(siblings) > 1 else ""))
        node = node.parent
    return " > ".join(reversed(parts))


class RecognitionSession:
    def __init__(self, url: str, profile: Path, *, knowledge=None, headless=False, confirmation_timeout=4):
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            raise ValueError("Expected HTTP(S) URL without credentials")
        self.url, self.profile = url, profile
        self.hosts = [parsed.hostname]
        self.knowledge, self.headless = knowledge, headless
        self.confirmation_timeout = confirmation_timeout
        self.result = Recognition()
        self.recorder = Recorder(url, profile)
        self.context = self.page = self.playwright = None
        self.browser_attempted = self.http_attempted = False
        self.read_only = True
        self.build = None
        self.history = []
        self.closed = False
        self.manual = False
        self.manual_finished = False
        self.sent_writes = set()

    def state(self, value):
        self.result.state = value
        self.history.append(str(value))

    async def start(self):
        from playwright.async_api import async_playwright

        self.playwright = await async_playwright().start()
        try:
            self.profile.mkdir(parents=True, exist_ok=True)
            self.context = await self.playwright.chromium.launch_persistent_context(
                str(self.profile),
                service_workers="block",
                **chromium_options(self.headless, self.playwright.chromium.executable_path),
            )
            await self.context.route("**/*", self.gate)
            await self.context.route_web_socket("**/*", lambda socket: socket.close())
            await self.recorder.attach(self.context)
            self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
            await self.page.goto(self.url, wait_until="domcontentloaded")
        except BaseException:
            await self.close()
            raise
        return self

    async def gate(self, route):
        request = route.request
        parsed = urlsplit(request.url)
        allowed = parsed.hostname in self.hosts and parsed.scheme in {"http", "https"}
        if urlsplit(self.url).scheme == "https" and parsed.scheme != "https":
            allowed = False
        if not allowed:
            if "BLOCKED_HOST" not in self.result.diagnostics:
                self.result.diagnostics.append("BLOCKED_HOST")
            await route.abort()
        elif self.read_only and request.method not in {"GET", "HEAD", "OPTIONS"}:
            await route.abort()
        else:
            if request.method not in {"GET", "HEAD", "OPTIONS"}:
                identity = hashlib.sha256(
                    (request.method + request.url).encode() + b"\0" + (request.post_data_buffer or b"")
                ).hexdigest()
                if identity in self.sent_writes:
                    await route.abort()
                    self.result.diagnostics.append("ALREADY_ATTEMPTED")
                    return
                self.sent_writes.add(identity)
            await route.continue_()

    async def recognize(self):
        if self.closed:
            raise ValueError("Session closed")
        self.read_only = True
        self.result = Recognition(diagnostics=["READ_ONLY"])
        detector = UniversalFormDetector()
        forms = await detector.inspect(self.page, self.hosts)
        if not any(all(r in f.fields for r in ("title", "body", "submit")) for f in forms):
            for locator in (
                self.page.get_by_role("button", name=OPEN_FORM),
                self.page.get_by_role("link", name=OPEN_FORM),
            ):
                if await locator.count() == 1:
                    if await locator.get_attribute("type") == "submit":
                        continue
                    await locator.click()
                    try:
                        await self.page.wait_for_selector(
                            'textarea,[contenteditable="true"],iframe', timeout=2000
                        )
                    except Exception:
                        pass
                    forms = await detector.inspect(self.page, self.hosts)
                    break
        html = await self.page.content()
        engine, confidence, _ = EngineDetector().detect(html)
        self.result.engine, self.result.confidence.engine = engine, confidence
        self.result.forms = forms
        if self.knowledge:
            for form in forms:
                if self.knowledge.suggest(form):
                    form.confidence = detector.confidence(form.fields)
        self.result.auth_required = await self.page.locator('input[type="password"]').count() > 0
        if self.result.auth_required:
            self.result.diagnostics.append("AUTH_REQUIRED")
        complete = [f for f in forms if all(k in f.fields for k in ("title", "body", "submit"))]
        if not complete:
            self.result.diagnostics.append("NO_FORM")
            self.state(LearningState.LEARNING_REQUIRED)
        elif len(complete) > 1 and complete[0].confidence - complete[1].confidence < 0.1:
            self.result.diagnostics.append("AMBIGUOUS_FORM")
            self.state(LearningState.LEARNING_REQUIRED)
        else:
            self.select(complete[0].key)
            await self.highlight()
        return self.result

    async def highlight(self):
        form = next((f for f in self.result.forms if f.key == self.result.selected), None)
        if form:
            for role in ("title", "body", "submit"):
                ref = form.fields[role]
                await (
                    self.page.frames[ref.frame]
                    .locator(ref.selector)
                    .evaluate("e => {e.style.outline='3px solid #2374d8'; e.style.outlineOffset='2px';}")
                )

    def select(self, key):
        form = next(f for f in self.result.forms if f.key == key)
        if not all(r in form.fields for r in ("title", "body", "submit")):
            raise ValueError("Incomplete form")
        self.result.selected = key
        self.result.confidence.form = form.confidence
        self.result.confidence.fields = min(
            f.confidence for r, f in form.fields.items() if r in {"title", "body"}
        )
        if self.knowledge:
            rule = self.knowledge.match(form)
            if rule:
                self.result.rule_id = rule.id
                # A rule improves ranking evidence, never publication or HTTP verification.
                self.result.confidence.form = min(0.98, form.confidence + 0.02)
                form.evidence.append("learned-editor-structure")
        self.result.diagnostics = [
            c for c in self.result.diagnostics if c not in {"NO_FORM", "AMBIGUOUS_FORM"}
        ]
        self.state(LearningState.DETECTED)

    async def fill(self, title, body):
        form = next(f for f in self.result.forms if f.key == self.result.selected)
        if not title.strip() or not body.strip() or title == body:
            raise ValueError("CONTENT_REQUIRED")
        for role, value in (("title", title), ("body", body)):
            ref = form.fields[role]
            await self.page.frames[ref.frame].locator(ref.selector).fill(value)
        self.recorder.capture.title_marker = title
        self.recorder.capture.body_marker = body
        self.recorder.capture.body_format = (
            "html" if "contenteditable" in form.fields["body"].kind else "text"
        )
        self.recorder.capture.start_url = self.page.url
        return form

    async def test_publish(self, title, body, project_id, *, confirmed=False, existing_site=None):
        if not confirmed:
            raise PermissionError("CONSENT_REQUIRED")
        if self.browser_attempted:
            raise ValueError("ALREADY_ATTEMPTED")
        if self.result.state != LearningState.DETECTED:
            raise ValueError("NO_FORM")
        self.check_key()
        form = await self.fill(title, body)
        self.browser_attempted = True
        self.recorder.capture.action_id = uuid.uuid4().hex
        self.read_only = False
        try:
            ref = form.fields["submit"]
            await self.page.frames[ref.frame].locator(ref.selector).click(timeout=5000, no_wait_after=True)
            await self.confirm_and_learn(project_id, existing_site=existing_site)
        except Exception:
            self.result.diagnostics.append("NO_PERMALINK")
            self.state(LearningState.UNKNOWN)
        finally:
            self.read_only = True
        return self.result

    async def session_state(self):
        for _ in range(3):
            if not self.recorder.pending:
                break
            await asyncio.wait(list(self.recorder.pending), timeout=0.25)
        state = await self.context.storage_state()
        state["cookies"] = [
            c
            for c in state.get("cookies", [])
            if any(
                h == c["domain"].lstrip(".") or h.endswith("." + c["domain"].lstrip(".")) for h in self.hosts
            )
        ]
        state["origins"] = [
            o for o in state.get("origins", []) if urlsplit(o["origin"]).netloc == urlsplit(self.url).netloc
        ]
        for request in self.recorder.capture.requests:
            if urlsplit(request["url"]).netloc == urlsplit(self.url).netloc:
                state.setdefault("headers", {}).update(
                    {
                        k: v
                        for k, v in request.get("headers", {}).items()
                        if k in {"authorization", "x-api-key", "x-auth-token"}
                    }
                )
        self.recorder.capture.session = state
        return state

    async def confirm_and_learn(self, project_id, *, existing_site=None):
        deadline = asyncio.get_running_loop().time() + self.confirmation_timeout
        capture = self.recorder.capture
        while asyncio.get_running_loop().time() < deadline:
            await self.session_state()
            candidate = await self.find_confirmation()
            if candidate:
                capture.result_url, capture.confirmation = candidate
                capture.browser_verified = True
                self.result.confidence.publication = 0.95
                self.result.confidence.result = 1
                self.state(LearningState.BROWSER_VERIFIED)
                form = next((f for f in self.result.forms if f.key == self.result.selected), None)
                if form is None and self.manual:
                    form = self.learn_manual_fields()
                if self.knowledge and form:
                    self.result.rule_id = self.knowledge.propose(form, manual=self.manual)
                try:
                    self.build = HTTPWorkflowLearner().learn(
                        capture, project_id, rule_id=self.result.rule_id, existing_site=existing_site
                    )
                except (ValueError, TypeError, KeyError):
                    self.result.diagnostics.append("UNKNOWN_DYNAMIC")
                    self.state(LearningState.LEARNING_REQUIRED)
                    return
                self.result.diagnostics.extend(self.build.diagnostics)
                state = self.build.site["recognition"]["state"]
                self.state(LearningState(state))
                if state == "HTTP_CANDIDATE":
                    self.result.diagnostics.append("HTTP_UNVERIFIED")
                return
            failed = any(
                r["status"] >= 400 or '"success":false' in r["body"].replace(" ", "").lower()
                for r in capture.responses
                if r.get("action_id") == capture.action_id
            )
            if failed:
                self.state(LearningState.FAILED)
                self.result.diagnostics.append("PUBLICATION_REJECTED")
                return
            await asyncio.sleep(0.1)
        self.result.diagnostics.append("NO_PERMALINK")
        self.result.diagnostics.extend(NetworkAnalyzer().analyze(capture).diagnostics)
        self.state(LearningState.UNKNOWN)

    def learn_manual_fields(self):
        """Observe ordinary operator input on an unknown form; values never enter the rule database."""
        capture, fields = self.recorder.capture, {}
        for action in capture.actions:
            if action.get("event") != "field-input":
                continue
            for role, value in (("title", capture.title_marker), ("body", capture.body_marker)):
                if action.get("value") == value:
                    kind = ("iframe:" if action.get("iframe") else "") + action["kind"]
                    fields[role] = FieldRef(role, action["selector"], 0, kind, 1, action.get("position", 0))
        if not all(role in fields for role in ("title", "body")):
            return None
        fields["submit"] = FieldRef("submit", "observed-write", 0, "button", 1)
        capture.body_format = "html" if "contenteditable" in fields["body"].kind else "text"
        return FormCandidate("manual-observation", fields, 1, evidence=["operator-demonstrated-fields"])

    async def find_confirmation(self):
        capture = self.recorder.capture
        options = []
        # AJAX result URLs are associated with the observed write response, not a generic HTTP 200.
        for response in reversed(capture.responses):
            if (
                response["method"] in {"POST", "PUT", "PATCH"}
                and response.get("action_id") == capture.action_id
            ):
                try:
                    payload = json.loads(response["body"])
                    for path, value in leaves(payload):
                        if path.rsplit(".", 1)[-1] in {"url", "permalink", "location"} and isinstance(
                            value, str
                        ):
                            options.append((urljoin(response["url"], value), {"url_path": path}))
                except ValueError:
                    soup = BeautifulSoup(response["body"], "html.parser")
                    link = soup.select_one('a[rel~="bookmark"][href]')
                    if link:
                        options.append(
                            (urljoin(response["url"], link["href"]), {"link_selector": 'a[rel~="bookmark"]'})
                        )
        if self.page.url != capture.start_url:
            options.append((self.page.url, {}))
        for url, extra in options:
            try:
                confirmation = await self.read_confirmation(url)
                if confirmation:
                    return url, {**extra, **confirmation}
            except (ValueError, httpx.HTTPError, KeyError, WorkflowError):
                continue
        return None

    async def read_confirmation(self, url):
        site = Site(id="verification", base_url=self.url, allowed_hosts=self.hosts, status="HTTP_READY")
        placeholder = Workflow(
            steps=[
                Step(id="read", op="request", url=url, save_as="r"),
                Step(id="check", op="verify", source="r", kind="contains", expected="placeholder"),
            ]
        )
        runtime = Runtime(
            Project(site, placeholder, "browser-verification", encrypt_session(self.recorder.capture.session))
        )
        runtime.check_url(url)
        async with httpx.AsyncClient(timeout=10, follow_redirects=False, trust_env=False) as client:
            response = await runtime.request(client, placeholder.steps[0], {}, runtime.load_auth(client))
        soup = BeautifulSoup(response.text, "html.parser")
        selectors = {}
        for role, expected in (
            ("title", self.recorder.capture.title_marker),
            ("body", self.recorder.capture.body_marker),
        ):
            norm = " ".join(expected.split())
            nodes = [
                n
                for n in soup.find_all(True)
                if n.name not in {"script", "style", "input", "textarea"}
                and not n.find_parent("form")
                and not n.has_attr("contenteditable")
                and not n.has_attr("hidden")
                and " ".join(n.get_text(strip=True).split()) == norm
            ]
            if not nodes:
                return None
            # Pick a small content node; hidden form fields or returned editor values do not qualify.
            node = min(nodes, key=lambda n: len(str(n)))
            selectors[role + "_selector"] = css_path(node)
        return selectors

    async def verify_http(self, title, body, *, confirmed=False):
        if not confirmed:
            raise PermissionError("CONSENT_REQUIRED")
        if self.http_attempted:
            raise ValueError("ALREADY_ATTEMPTED")
        if self.result.state != LearningState.HTTP_CANDIDATE or not self.build:
            raise ValueError("HTTP_UNVERIFIED")
        self.http_attempted = True
        self.read_only = True
        site = Site.model_validate({**self.build.site, "status": "HTTP_READY", "recognition": None})
        session = encrypt_session(self.build.session) if self.build.session else None
        trial = Project(site, Workflow.model_validate(self.build.workflow), "verification-trial", session)
        result = await Runtime(trial).run({"title": title, "body": body, "target_url": self.url})
        if result.status == "SUCCEEDED":
            self.state(LearningState.HTTP_VERIFIED)
            self.result.confidence.http_verified = True
            self.result.diagnostics = [c for c in self.result.diagnostics if c != "HTTP_UNVERIFIED"]
            self.build.site["status"] = "HTTP_READY"
            self.build.site["recognition"].update(state="HTTP_VERIFIED", http_verified=True)
        else:
            self.state(LearningState.UNKNOWN if result.status == "UNKNOWN" else LearningState.FAILED)
            self.result.diagnostics.append("HTTP_FAILED")
            self.build.site["recognition"]["state"] = str(self.result.state)
        if self.knowledge and self.result.rule_id:
            self.knowledge.result(self.result.rule_id, result.status == "SUCCEEDED")
        return result

    async def begin_manual(self, title, body, *, confirmed=False):
        if not confirmed:
            raise PermissionError("CONSENT_REQUIRED")
        if self.browser_attempted:
            raise ValueError("ALREADY_ATTEMPTED")
        if not title.strip() or not body.strip() or title == body:
            raise ValueError("CONTENT_REQUIRED")
        self.check_key()
        if self.result.selected:
            await self.fill(title, body)
        else:
            self.recorder.capture.title_marker, self.recorder.capture.body_marker = title, body
        self.manual, self.browser_attempted = True, True
        self.recorder.capture.action_id = uuid.uuid4().hex
        self.read_only = False
        self.result.diagnostics.append("MANUAL_READY")

    @staticmethod
    def check_key():
        try:
            encrypt_session({"cookies": []})
        except (ValueError, TypeError):
            raise ValueError("SESSION_KEY_INVALID") from None

    async def finish_manual(self, project_id, *, existing_site=None):
        self.read_only = True
        if not self.manual:
            raise ValueError("Manual learning is not active")
        if self.manual_finished:
            raise ValueError("ALREADY_ATTEMPTED")
        self.manual_finished = True
        await self.confirm_and_learn(project_id, existing_site=existing_site)
        return self.result

    def save(self, output):
        if (
            self.result.state not in {LearningState.HTTP_CANDIDATE, LearningState.HTTP_VERIFIED}
            or not self.build
        ):
            raise ValueError("Unconfirmed/unsafe workflow cannot be exported")
        export(self.build, output)
        (output / "recognition.json").write_text(
            json.dumps({**self.result.summary(), "history": self.history}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        from ..project import mark_ready

        mark_ready(output)

    async def close(self):
        self.closed = True
        if self.context:
            await self.context.close()
            self.context = None
        if self.recorder.pending:
            await asyncio.gather(*list(self.recorder.pending), return_exceptions=True)
        if self.playwright:
            await self.playwright.stop()
            self.playwright = None
