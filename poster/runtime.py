"""The executor imports no browser modules. All redirects pass through the host gate."""

import asyncio
import html
import re
from collections import defaultdict
from dataclasses import dataclass, field
from http.cookiejar import Cookie
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

import httpx
from bs4 import BeautifulSoup
from cryptography.fernet import InvalidToken

from .auth import decrypt_bytes
from .models import Step
from .project import Project

TOKEN = re.compile(r"{{\s*([a-zA-Z_][\w.]*)(?:\|(html|html_lines|url))?\s*}}")


class WorkflowError(Exception):
    pass


class AuthenticationExpired(WorkflowError):
    pass


class AmbiguousWrite(WorkflowError):
    pass


def render(value, context):
    if isinstance(value, str):

        def resolve(m):
            data = context
            for key in m[1].split("."):
                if not isinstance(data, dict) or key not in data:
                    raise WorkflowError(f"Missing variable: {m[1]}")
                data = data[key]
            if isinstance(data, (httpx.Response, dict, list)):
                raise WorkflowError("Variable must be a scalar")
            if m[2] == "html":
                return html.escape(str(data), quote=False)
            if m[2] == "html_lines":
                return html.escape(str(data), quote=False).replace("\r\n", "\n").replace("\n", "<br>")
            if m[2] == "url":
                return quote(str(data), safe="")
            return data

        remainder = TOKEN.sub("", value)
        if "{{" in remainder or "}}" in remainder:
            raise WorkflowError("Unsupported template expression")
        exact = TOKEN.fullmatch(value)
        if exact:
            return resolve(exact)
        return TOKEN.sub(lambda m: str(resolve(m)), value)
    if isinstance(value, dict):
        return {k: render(v, context) for k, v in value.items()}
    if isinstance(value, list):
        return [render(v, context) for v in value]
    return value


def safe_url(url: str) -> str:
    u = urlsplit(str(url))
    return urlunsplit((u.scheme, u.netloc.rsplit("@", 1)[-1], u.path, "", ""))


def result_permalink(url: str) -> str:
    """Keep numeric forum identities needed for a useful permalink, drop secrets."""
    from urllib.parse import parse_qsl, urlencode

    parsed = urlsplit(str(url))
    pairs = parse_qsl(re.sub(r";(?=[A-Za-z_][A-Za-z0-9_]*=)", "&", parsed.query))
    pairs = [
        (
            k,
            v.split(".", 1)[0] + ".0"
            if k == "topic" and re.fullmatch(r"\d+\.(?:msg\d+|new)(?:;boardseen)?", v)
            else v,
        )
        for k, v in pairs
    ]
    permitted = {"t", "p", "f", "tid", "pid", "fid", "topic", "board"}
    query = urlencode([(k, v) for k, v in pairs if k in permitted and re.fullmatch(r"\d+(?:\.\d+)?", v)])
    return urlunsplit((parsed.scheme, parsed.netloc.rsplit("@", 1)[-1], parsed.path, query, ""))


def extract(step: Step, response: httpx.Response):
    if step.kind in {"css_text", "css_attribute"}:
        node = BeautifulSoup(response.text, "html.parser").select_one(step.selector)
        if node is None:
            raise WorkflowError("Expected confirmation/extraction element is absent")
        value = (
            node.get_text(" ", strip=True)
            if step.kind == "css_text" and step.normalize == "whitespace"
            else node.get_text(strip=True)
            if step.kind == "css_text"
            else node.get(step.attribute)
        )
        if value is None or value == "":
            raise WorkflowError("Expected element value is absent")
        return value
    if step.kind == "json_path":
        value = response.json()
        for key in step.path.removeprefix("$.").split("."):
            value = value[int(key)] if isinstance(value, list) else value[key]
        return value
    if step.kind == "header":
        value = response.headers.get(step.attribute)
        if not value:
            raise WorkflowError("Expected response header is absent")
        return value
    if step.kind == "regex":
        match = re.search(step.pattern, response.text[:1_000_000])
        if not match:
            raise WorkflowError("Regex did not match")
        return match.group(1) if match.groups() else match.group(0)
    if step.kind == "contains":
        return str(step.expected) in response.text
    if step.kind == "status":
        return response.status_code
    raise WorkflowError("Unsupported extraction")


@dataclass
class Result:
    status: str
    result_url: str | None = None
    reason: str = ""
    operations: list[dict] = field(default_factory=list)


class Runtime:
    def __init__(self, project: Project, transport=None, host_limits=None, *, strict_origin=False):
        self.project = project
        self.transport = transport
        self.operations = []
        self.wrote = False
        self.verified = False
        self.result_url = None
        self.strict_origin = strict_origin or bool(project.site.recognition)
        self.host_limits = (
            host_limits if host_limits is not None else defaultdict(lambda: asyncio.Semaphore(1))
        )

    def check_url(self, url):
        parsed = urlsplit(str(url))
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.username
            or parsed.password
            or not parsed.hostname
            or parsed.hostname.lower() not in [h.lower() for h in self.project.site.allowed_hosts]
        ):
            raise WorkflowError("Request/redirect host is outside allowed_hosts")
        if urlsplit(self.project.site.base_url).scheme == "https" and parsed.scheme != "https":
            raise WorkflowError("HTTPS downgrade is forbidden")
        if self.strict_origin:
            base = urlsplit(self.project.site.base_url)

            def address(p):
                return p.scheme, p.hostname, p.port or (443 if p.scheme == "https" else 80)

            if address(parsed) != address(base):
                raise WorkflowError("Recognized workflow origin is outside the verified origin")

    def load_auth(self, client):
        if not self.project.session:
            return {}
        state = decrypt_bytes(self.project.session)
        for c in state.get("cookies", []):
            domain = c["domain"]
            if not any(
                h == domain.lstrip(".") or h.endswith("." + domain.lstrip("."))
                for h in self.project.site.allowed_hosts
            ):
                continue
            client.cookies.jar.set_cookie(
                Cookie(
                    0,
                    c["name"],
                    c["value"],
                    None,
                    False,
                    domain,
                    domain.startswith("."),
                    domain.startswith("."),
                    c.get("path", "/"),
                    True,
                    c.get("secure", False),
                    int(c["expires"]) if c.get("expires", -1) > 0 else None,
                    False,
                    None,
                    None,
                    {},
                    False,
                )
            )
        return {
            k: v
            for k, v in state.get("headers", {}).items()
            if k.lower() in {"authorization", "x-api-key", "x-auth-token", "user-agent"}
        }

    async def request(self, client, step, context, auth_headers):
        site = self.project.site
        url = urljoin(site.base_url, render(step.url, context))
        method = step.method
        headers = {**site.headers, **render(step.headers, context)}
        kwargs = {}
        if step.form is not None:
            kwargs["data"] = render(step.form, context)
        if step.json_body is not None:
            kwargs["json"] = render(step.json_body, context)
        if step.multipart is not None:
            kwargs["files"] = {k: (None, v) for k, v in render(step.multipart, context).items()}
        for _redirect in range(11):
            self.check_url(url)
            current_headers = dict(headers)
            if urlsplit(url).netloc == urlsplit(site.base_url).netloc:
                current_headers.update(auth_headers)
            attempts = site.get_retries + 1 if method in {"GET", "HEAD"} else 1
            for attempt in range(attempts):
                try:
                    if method not in {"GET", "HEAD", "OPTIONS"}:
                        self.wrote = True
                    # Stream with a cap: an untrusted project cannot download arbitrary-sized responses.
                    async with self.host_limits[urlsplit(url).hostname]:
                        async with client.stream(method, url, headers=current_headers, **kwargs) as response:
                            chunks, size = [], 0
                            async for chunk in response.aiter_bytes():
                                size += len(chunk)
                                if size > 5_000_000:
                                    raise WorkflowError("Response exceeds 5 MB")
                                chunks.append(chunk)
                            decoded_headers = {
                                k: v
                                for k, v in response.headers.items()
                                if k.lower() not in {"content-encoding", "content-length"}
                            }
                            response = httpx.Response(
                                response.status_code,
                                headers=decoded_headers,
                                content=b"".join(chunks),
                                request=response.request,
                            )
                    if response.status_code in {429, 502, 503, 504} and attempt + 1 < attempts:
                        await asyncio.sleep(site.retry_delay * (2**attempt))
                        continue
                    break
                except httpx.TransportError as exc:
                    if method not in {"GET", "HEAD", "OPTIONS"}:
                        raise AmbiguousWrite("Write outcome unknown; no automatic resubmission") from exc
                    if attempt + 1 == attempts:
                        raise WorkflowError("HTTP read transport failure") from exc
                    await asyncio.sleep(site.retry_delay * (2**attempt))
            self.operations.append(
                {"step": step.id, "method": method, "status": response.status_code, "url": safe_url(url)}
            )
            if response.status_code in {401, 403}:
                raise AuthenticationExpired("Authentication/authorization rejected (401/403)")
            if response.status_code in {301, 302, 303, 307, 308} and "location" in response.headers:
                url = urljoin(url, response.headers["location"])
                self.check_url(url)
                if response.status_code == 303 or (response.status_code in {301, 302} and method == "POST"):
                    method, kwargs = "GET", {}
                elif method not in {"GET", "HEAD"}:
                    # 307/308 can resend writes: operator must express these as explicit steps.
                    raise AmbiguousWrite("Write-preserving redirect requires review")
                continue
            if response.status_code >= 400:
                if self.wrote:
                    raise AmbiguousWrite("Write returned an error; confirm result before retry")
                raise WorkflowError("HTTP read failed")
            return response
        raise WorkflowError("Too many redirects")

    async def steps(self, steps, context, client, auth_headers):
        for step in steps:
            if step.op == "request":
                context[step.save_as] = await self.request(client, step, context, auth_headers)
            elif step.op == "set":
                context.update(render(step.values, context))
            elif step.op == "wait":
                await asyncio.sleep(step.seconds)
            else:
                response = context.get(step.source)
                if not isinstance(response, httpx.Response):
                    raise WorkflowError("Extraction source is not an HTTP response")
                if step.kind == "cookie":
                    host = response.url.host
                    values = {
                        c.value
                        for c in client.cookies.jar
                        if c.name == step.attribute
                        and (host == c.domain.lstrip(".") or host.endswith("." + c.domain.lstrip(".")))
                    }
                    if len(values) != 1:
                        raise WorkflowError("Session cookie is absent or ambiguous")
                    actual = values.pop()
                else:
                    actual = extract(
                        step.model_copy(update={"expected": render(step.expected, context)}), response
                    )
                if step.op == "extract":
                    context[step.save_as] = actual
                else:
                    expected = render(step.expected, context)
                    if step.normalize == "whitespace":
                        actual = " ".join(str(actual).split())
                        expected = " ".join(str(expected).split()) if expected is not None else None
                    passed = (
                        bool(actual)
                        if expected is None or step.kind == "contains"
                        else str(actual) == str(expected)
                    )
                    if not passed:
                        raise WorkflowError("Assertion/verification failed")
                    if step.op == "verify":
                        self.verified = True
                        if step.result_url:
                            self.result_url = result_permalink(str(response.url))

    async def run(self, variables: dict, *, dry_run=False, recovery=False) -> Result:
        site = self.project.site
        if site.status != "HTTP_READY" or not site.enabled or site.mode != "http":
            return Result("REVIEW_REQUIRED", reason="Project is not enabled HTTP_READY")
        if site.recognition and (
            site.recognition.state != "HTTP_VERIFIED" or not site.recognition.http_verified
        ):
            return Result(
                "REVIEW_REQUIRED", reason="Recognized HTTP candidate has not been independently verified"
            )
        context = dict(variables)
        steps = self.project.workflow.recovery if recovery else self.project.workflow.steps
        if recovery and not steps:
            return Result("UNKNOWN", reason="No read-only recovery workflow; manual verification required")
        if dry_run:
            # No requests at all. Symbolic extracted values validate references for subsequent steps.
            try:
                for s in steps:
                    for v in (s.url, s.form, s.json_body, s.multipart, s.headers, s.values, s.expected):
                        render(v, context)
                    if s.op == "set":
                        context.update(render(s.values, context))
                    if s.op == "request":
                        self.check_url(urljoin(site.base_url, render(s.url, context)))
                    if s.save_as:
                        context[s.save_as] = "DRY_RUN_VALUE"
                return Result("REVIEW_REQUIRED", reason="Dry run valid; no network requests or success claim")
            except WorkflowError as exc:
                return Result("FAILED", reason=str(exc))
        try:
            async with httpx.AsyncClient(
                timeout=site.timeout, follow_redirects=False, transport=self.transport, trust_env=False
            ) as client:
                auth_headers = self.load_auth(client)
                try:
                    await self.steps(steps, context, client, auth_headers)
                except (AmbiguousWrite, WorkflowError) as exc:
                    if isinstance(exc, AuthenticationExpired):
                        raise
                    if self.wrote and self.project.workflow.recovery and not recovery:
                        self.verified = False
                        try:
                            await self.steps(
                                self.project.workflow.recovery, dict(variables), client, auth_headers
                            )
                        except (WorkflowError, KeyError, ValueError, httpx.HTTPError):
                            return Result(
                                "UNKNOWN",
                                reason="Read-only recovery could not confirm write",
                                operations=self.operations,
                            )
                        if self.verified:
                            return Result(
                                "SUCCEEDED",
                                self.result_url,
                                "Confirmed by read-only recovery",
                                self.operations,
                            )
                    raise
            return Result(
                "SUCCEEDED" if self.verified else "UNKNOWN", self.result_url, operations=self.operations
            )
        except AuthenticationExpired as exc:
            return Result("AUTH_EXPIRED", reason=str(exc), operations=self.operations)
        except InvalidToken:
            return Result("REVIEW_REQUIRED", reason="Session encryption key does not match session.enc")
        except (WorkflowError, ValueError, KeyError, TypeError, httpx.HTTPError) as exc:
            # Never include server bodies, token values or exception URLs in diagnostics.
            reason = str(exc) if isinstance(exc, WorkflowError) else "Invalid extraction/session data"
            return Result(
                "UNKNOWN" if self.wrote or recovery else "FAILED", reason=reason, operations=self.operations
            )
