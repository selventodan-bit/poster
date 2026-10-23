"""A bounded, consent-gated browser session; the executor stays browser-free."""

import asyncio
import json
import uuid
import time
import os
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup

from ..auth import encrypt_session
from ..models import Site, Step, Workflow
from ..project import Project
from ..recorder.builder import export
from ..recorder.capture import Recorder, chromium_options
from ..runtime import AuthenticationExpired, Runtime, WorkflowError
from .detector import EngineDetector
from .forms import UniversalFormDetector
from .editors import adapter_for
from .navigation import NavigationDiscovery
from .policy import ResourcePolicy, SubmissionScope, dangerous_url, origin
from .learner import HTTPWorkflowLearner
from .network import NetworkAnalyzer, rejected
from .network import leaves
from .types import FieldRef, FormCandidate, LearningState, Recognition
from .transfer import preflight


def css_path(node):
    parts = []
    while node and node.name not in {"[document]", "html"}:
        siblings = [n for n in node.parent.find_all(node.name, recursive=False)] if node.parent else [node]
        parts.append(node.name + (f":nth-of-type({siblings.index(node) + 1})" if len(siblings) > 1 else ""))
        node = node.parent
    return " > ".join(reversed(parts))


class RecognitionSession:
    def __init__(
        self,
        url: str,
        profile: Path,
        *,
        knowledge=None,
        headless=False,
        confirmation_timeout=None,
        permalink_timeout=20,
        static_origins=None,
        locales=None,
    ):
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
        self.confirmation_timeout = float(
            confirmation_timeout
            if confirmation_timeout is not None
            else os.environ.get("POSTER_CONFIRMATION_TIMEOUT", "45")
        )
        self.permalink_timeout = float(permalink_timeout)
        if not 0 < self.confirmation_timeout <= 600 or not 0 < self.permalink_timeout <= 600:
            raise ValueError("Confirmation deadlines must be between 0 and 600 seconds")
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
        self.scope = None
        self.resources = ResourcePolicy(static_origins)
        self.navigation = NavigationDiscovery(locales=locales, knowledge=knowledge)
        self.locales = locales
        self.login_scope = None
        self.transfer_rule = None

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
        allowed = origin(request.url) == origin(self.url)
        if urlsplit(self.url).scheme == "https" and parsed.scheme != "https":
            allowed = False
        if not allowed:
            if await self.resources.fulfill_static(route):
                return
            if "BLOCKED_HOST" not in self.result.diagnostics:
                self.result.diagnostics.append("BLOCKED_HOST")
            await route.abort()
        elif dangerous_url(request.url):
            await route.abort()
        elif (
            self.login_scope
            and request.method == "POST"
            and time.monotonic() < self.login_scope["deadline"]
            and request.url == self.login_scope["url"]
            and not self.login_scope["used"]
        ):
            self.login_scope["used"] = True
            await route.continue_()
        elif self.read_only and request.method not in {"GET", "HEAD", "OPTIONS"}:
            await route.abort()
        else:
            if request.method not in {"GET", "HEAD", "OPTIONS"}:
                if self.recorder.pending:
                    await asyncio.wait(list(self.recorder.pending), timeout=0.25)
                if not self.scope or not self.scope.allows(request):
                    recorded = self.recorder.request_records.get(request)
                    if recorded:
                        recorded["blocked"] = True
                    await route.abort()
                    self.result.diagnostics.append("WRITE_SCOPE")
                    return
                # Recorder's request event precedes routing; bind only the admitted request.
                recorded = self.recorder.request_records.get(request)
                if recorded:
                    self.scope.allowed_ids.add(recorded["id"])
            await route.continue_()

    async def authorize_login(self, *, confirmed=False):
        if not confirmed:
            raise PermissionError("CONSENT_REQUIRED")
        if self.browser_attempted:
            raise ValueError("ALREADY_ATTEMPTED")
        passwords = self.page.locator('input[type="password"]:visible')
        if await passwords.count() != 1:
            raise ValueError("AUTH_REQUIRED")
        action = await passwords.evaluate(
            "e=>e.form?new URL(e.form.getAttribute('action')||location.href,e.baseURI).href:null"
        )
        explicit_action = await passwords.evaluate("e=>e.form?.getAttribute('action')")
        if not explicit_action:
            technologies = EngineDetector().report(
                await self.page.content(),
                url=self.page.url,
                requests=self.recorder.capture.requests,
                responses=self.recorder.capture.responses,
            )
            if any(t["family"] == "flarum" for t in technologies):
                base = await self.page.evaluate("window.app?.forum?.attribute('baseUrl') || location.origin")
                action = urljoin(base.rstrip("/") + "/", "login")
        if not action or origin(action) != origin(self.url):
            raise ValueError("BLOCKED_HOST")
        self.login_scope = {"url": action, "deadline": time.monotonic() + 120, "used": False}

    async def recognize(self):
        if self.closed:
            raise ValueError("Session closed")
        self.read_only = True
        self.result = Recognition(diagnostics=["READ_ONLY"])
        detector = UniversalFormDetector(self.locales)
        forms = await self.navigation.discover(self.page, lambda: detector.inspect(self.page, self.hosts))
        # Editors may replace the textarea with an iframe after DOMContentLoaded.
        # Resnapshot during that transition; never retain stale field selectors.
        for _ in range(5):
            if any({"title", "body", "submit"} <= f.fields.keys() for f in forms):
                break
            await asyncio.sleep(0.15)
            forms = await detector.inspect(self.page, self.hosts)
        self.result.navigation = self.navigation.trace
        html = await self.page.content()
        technologies = EngineDetector().report(
            html,
            url=self.page.url,
            requests=self.recorder.capture.requests,
            responses=self.recorder.capture.responses,
        )
        self.result.technologies = technologies
        engine, confidence = (
            (technologies[0]["family"], technologies[0]["confidence"]) if technologies else ("unknown", 0)
        )
        self.result.engine, self.result.confidence.engine = engine, confidence
        self.result.forms = forms
        if self.knowledge:
            for form in forms:
                if self.knowledge.suggest(form):
                    form.confidence = detector.confidence(form.fields)
                if "knowledge-conflict" in form.evidence:
                    self.result.diagnostics.append("KNOWLEDGE_CONFLICT")
        self.result.auth_required = (
            await self.page.locator('input[type="password"]:visible').count() > 0
            or any(
                r["status"] in {401, 403} and origin(r["url"]) == origin(self.url)
                for r in self.recorder.capture.responses
            )
            or any(part in urlsplit(self.page.url).path.lower() for part in ("/login", "/sign-in", "/signin"))
        )
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
            if self.transfer_rule:
                self.result.transfer = await preflight(
                    self.page, complete[0], self.transfer_rule.http_templates[0]
                )
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
            reusable = self.knowledge.http_candidates(form, engine=self.result.engine)
            self.transfer_rule = reusable[0] if reusable else None
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
            await adapter_for(ref).fill(
                self.page.frames[ref.frame].locator(ref.selector),
                value,
                staged="previous-stage" in ref.features,
            )
        self.recorder.capture.title_marker = title
        self.recorder.capture.body_marker = body
        self.recorder.capture.body_format = (
            "html"
            if "contenteditable" in form.fields["body"].kind or form.fields["body"].editor != "native"
            else "text"
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
        ref = form.fields["submit"]
        control = (
            await self.page.frames[ref.frame]
            .locator(ref.selector)
            .evaluate(
                """e=>({name:e.name||'',value:e.value||'',action:e.form?
            new URL(e.getAttribute('formaction')||e.form.getAttribute('action')||location.href,e.baseURI).href:null})"""
            )
        )
        self.scope = SubmissionScope(
            self.url,
            self.recorder.capture,
            templates=self.transfer_rule.http_templates if self.transfer_rule else (),
            submitter=(control["name"], control["value"]) if control["name"] else None,
            action_url=urlsplit(control["action"])._replace(fragment="").geturl()
            if control["action"] and control["name"]
            else None,
        )
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
                        if k in {"authorization", "x-api-key", "x-auth-token", "user-agent"}
                    }
                )
        self.recorder.capture.session = state
        return state

    async def confirm_and_learn(self, project_id, *, existing_site=None):
        deadline = asyncio.get_running_loop().time() + self.confirmation_timeout
        capture = self.recorder.capture
        publication_received = None
        poll_interval = 0.1
        while asyncio.get_running_loop().time() < deadline:
            await self.session_state()
            writes = [
                r
                for r in capture.responses
                if r.get("action_id") == capture.action_id and r["method"] in {"POST", "PUT", "PATCH"}
            ]
            if writes and publication_received is None:
                publication_received = asyncio.get_running_loop().time()
            explicitly_rejected = any(
                r.get("action_id") == capture.action_id
                and r["method"] in {"POST", "PUT", "PATCH"}
                and rejected(r)
                for r in capture.responses
            )
            candidate = None
            # The overall deadline includes slow POST responses. Once an actual
            # response arrives, a separate bounded read-only permalink window starts.
            remaining = deadline - asyncio.get_running_loop().time()
            if publication_received is not None:
                remaining = min(
                    remaining,
                    publication_received + self.permalink_timeout - asyncio.get_running_loop().time(),
                )
            if remaining <= 0:
                break
            if writes and not explicitly_rejected:
                try:
                    async with asyncio.timeout(remaining):
                        candidate = await self.find_confirmation()
                except TimeoutError:
                    break
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
                    self.result.rule_id = self.knowledge.propose(
                        form,
                        manual=self.manual,
                        origin=str(origin(self.url)),
                        engine=self.result.engine,
                        navigation=self.result.navigation,
                        capture=capture,
                    )
                try:
                    learner = HTTPWorkflowLearner()
                    self.build = learner.learn(
                        capture,
                        project_id,
                        rule_id=self.result.rule_id,
                        existing_site=existing_site,
                        knowledge_rule=self.transfer_rule
                        or next((r for r in self.knowledge.rules() if r.id == self.result.rule_id), None)
                        if self.knowledge
                        else None,
                    )
                    self.result.transfer.update(learner.transfer_report)
                except (ValueError, TypeError, KeyError):
                    self.result.diagnostics.append("UNKNOWN_DYNAMIC")
                    self.state(LearningState.LEARNING_REQUIRED)
                    return
                self.result.diagnostics.extend(self.build.diagnostics)
                if self.knowledge and self.result.rule_id:
                    self.knowledge.observe_workflow(self.result.rule_id, self.build.workflow)
                    if self.result.technologies:
                        self.knowledge.observe_engine(self.result.rule_id, self.result.technologies[0])
                self.result.outcome = "publication_confirmed"
                state = self.build.site["recognition"]["state"]
                self.state(LearningState(state))
                if state == "HTTP_CANDIDATE":
                    self.result.diagnostics.append("HTTP_UNVERIFIED")
                else:
                    self.result.outcome = "workflow_not_built"
                return
            failed = any(rejected(r) for r in writes)
            if failed:
                self.state(LearningState.FAILED)
                self.result.diagnostics.append("PUBLICATION_REJECTED")
                self.result.outcome = "publication_rejected"
                if any(
                    r["status"] in {401, 403} and r.get("action_id") == capture.action_id
                    for r in capture.responses
                ):
                    self.result.diagnostics.append("AUTH_EXPIRED")
                    self.result.outcome = "auth_expired"
                if self.knowledge and self.result.rule_id:
                    self.knowledge.result(self.result.rule_id, False)
                return
            self.recorder.response_event.clear()
            try:
                await asyncio.wait_for(
                    self.recorder.response_event.wait(),
                    timeout=min(poll_interval, max(0.001, deadline - asyncio.get_running_loop().time())),
                )
            except TimeoutError:
                pass
            poll_interval = min(1, poll_interval * 1.5)
        self.result.diagnostics.append("NO_PERMALINK")
        self.result.diagnostics.append("CONFIRMATION_PENDING")
        self.result.diagnostics.extend(NetworkAnalyzer().analyze(capture).diagnostics)
        self.state(LearningState.UNKNOWN)
        self.result.outcome = (
            "auth_expired" if "AUTH_EXPIRED" in self.result.diagnostics else "confirmation_timeout"
        )

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
                    if not extra:
                        # Some native CMSes return a 200 interstitial and only
                        # then navigate via meta-refresh/JS. Bind its actual
                        # permalink anchor instead of verifying the interstitial.
                        target = urlsplit(url)._replace(fragment="").geturl()
                        for response in reversed(capture.responses):
                            if response.get("action_id") != capture.action_id or response["method"] not in {
                                "POST",
                                "PUT",
                                "PATCH",
                            }:
                                continue
                            soup = BeautifulSoup(response.get("body", ""), "html.parser")
                            links = [
                                n
                                for n in soup.select("a[href]")
                                if urlsplit(urljoin(response["url"], n["href"]))
                                ._replace(fragment="")
                                .geturl()
                                == target
                            ]
                            if links:
                                extra = {"link_selector": css_path(links[0])}
                                break
                            if self.result.engine == "flarum":
                                try:
                                    resource = json.loads(response["body"])["data"]
                                    identity = str(resource["id"])
                                    prefix = urlsplit(url).path.rsplit("/", 1)[0] + "/"
                                    tail = urlsplit(url).path.rsplit("/", 1)[-1]
                                    if (
                                        resource["type"] == "discussions"
                                        and identity.isdigit()
                                        and (tail == identity or tail.startswith(identity + "-"))
                                        and prefix.endswith("/d/")
                                    ):
                                        extra = {"identity_path": "data.id", "result_prefix": prefix}
                                        break
                                except (ValueError, KeyError, TypeError):
                                    pass
                    return url, {**extra, **confirmation}
            except AuthenticationExpired:
                self.result.diagnostics.append("AUTH_EXPIRED")
            except (ValueError, httpx.HTTPError, KeyError, WorkflowError):
                continue
        return None

    async def read_confirmation(self, url):
        if origin(url) != origin(self.url):
            return None
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
        if (
            response.status_code != 200
            or soup.select_one('input[type="password"]')
            or any(p in urlsplit(str(response.url)).path.lower() for p in ("/login", "/signin", "/sign-in"))
        ):
            if soup.select_one('input[type="password"]') or any(
                p in urlsplit(str(response.url)).path.lower() for p in ("/login", "/signin", "/sign-in")
            ):
                self.result.diagnostics.append("AUTH_EXPIRED")
            return None
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
                and not any(
                    p.name == "form"
                    and p.select_one('textarea,[contenteditable="true"],input[type="text"],input:not([type])')
                    for p in n.parents
                )
                and not n.has_attr("contenteditable")
                and not n.has_attr("hidden")
                and not n.find_parent(["script", "style", "textarea", "template", "dialog"])
                and not any(
                    p.has_attr("hidden")
                    or p.get("aria-hidden") == "true"
                    or p.has_attr("contenteditable")
                    or "display:none" in p.get("style", "").replace(" ", "")
                    for p in [n, *n.parents]
                    if getattr(p, "attrs", None)
                )
                and " ".join(n.get_text(" ", strip=True).split()) == norm
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
        if (
            not title.strip()
            or not body.strip()
            or title == self.recorder.capture.title_marker
            or body == self.recorder.capture.body_marker
        ):
            raise ValueError("DISTINCT_CONTENT")
        self.check_key()
        self.http_attempted = True
        self.read_only = True
        site = Site.model_validate({**self.build.site, "status": "HTTP_READY", "recognition": None})
        session = encrypt_session(self.build.session) if self.build.session else None
        trial = Project(site, Workflow.model_validate(self.build.workflow), "verification-trial", session)
        result = await Runtime(trial, strict_origin=True).run(
            {"title": title, "body": body, "target_url": self.url}
        )
        if result.status == "SUCCEEDED":
            self.state(LearningState.HTTP_VERIFIED)
            self.result.confidence.http_verified = True
            self.result.diagnostics = [c for c in self.result.diagnostics if c != "HTTP_UNVERIFIED"]
            self.build.site["status"] = "HTTP_READY"
            self.build.site["recognition"].update(state="HTTP_VERIFIED", http_verified=True)
        else:
            self.state(LearningState.UNKNOWN if result.status == "UNKNOWN" else LearningState.FAILED)
            self.result.diagnostics.append("HTTP_FAILED")
            self.result.outcome = (
                "auth_expired"
                if result.status == "AUTH_EXPIRED"
                else "http_unknown"
                if result.status == "UNKNOWN"
                else "http_failed"
            )
            if result.status == "AUTH_EXPIRED":
                self.result.diagnostics.append("AUTH_EXPIRED")
            self.build.site["recognition"]["state"] = str(self.result.state)
        if self.knowledge and self.result.rule_id:
            self.knowledge.result(
                self.result.rule_id, result.status == "SUCCEEDED", origin=str(origin(self.url))
            )
            if result.status == "SUCCEEDED":
                self.knowledge.record_template(
                    self.result.rule_id, self.recorder.capture, self.build.workflow
                )
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
        self.scope = SubmissionScope(self.url, self.recorder.capture)
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
