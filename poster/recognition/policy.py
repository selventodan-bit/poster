"""Browser resource and mutation policies. Consent grants a bounded content-linked transaction."""

import hashlib
import json
import re
from urllib.parse import urlsplit

import httpx

from .network import decode, request_values, response_values, text


def origin(url):
    parsed = urlsplit(url)
    return parsed.scheme, parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)


def dangerous_url(url):
    return bool(
        re.search(
            r"(?:[/=?&_-])(?:delete|logout|unsubscribe|vote|remove|settings|preferences|profile)(?:[/=?&_-]|$)",
            url,
            re.I,
        )
    )


class SubmissionScope:
    def __init__(self, url, capture, *, max_writes=6, templates=()):
        self.origin = origin(url)
        self.capture = capture
        self.max_writes = max_writes
        self.sent = set()
        self.allowed_ids = set()
        self.templates = templates

    def allows(self, request):
        if (
            origin(request.url) != self.origin
            or request.method not in {"POST", "PUT", "PATCH"}
            or dangerous_url(request.url)
        ):
            return False
        if len(self.sent) >= self.max_writes:
            return False
        raw = request.post_data_buffer or b""
        try:
            _, payload = decode({"content_type": request.headers.get("content-type", ""), "body": raw})
        except (ValueError, UnicodeError, LookupError):
            return False
        identity = hashlib.sha256(
            (request.method + request.url + "\0" + json.dumps(payload, sort_keys=True)).encode()
        ).hexdigest()
        if identity in self.sent:
            return False
        item = {"payload": payload, "url": request.url, "headers": request.headers}
        from .network import leaves

        content = any(
            isinstance(v, str)
            and (self.capture.title_marker in v or text(self.capture.body_marker) == text(v))
            for _, v in leaves(payload)
        )
        dependency = False
        for response in self.capture.responses:
            if response.get("request_id") not in self.allowed_ids or response.get("status", 500) >= 400:
                continue
            if any(
                str(value) == str(used) and str(value)
                for _, _, value in response_values(response)
                for _, _, used in request_values(item)
            ):
                dependency = True
        if self.sent and not dependency and self.templates:
            from .transfer import transition_allowed

            dependency = transition_allowed(self.templates, self.capture, request, self.allowed_ids)
        if (not self.sent and not content) or (self.sent and not dependency):
            return False
        self.sent.add(identity)
        return True


class ResourcePolicy:
    # Exact origins, not suffix/wildcard allowlists. Users may explicitly add another static origin.
    DEFAULT_STATIC = (
        "https://cdn.jsdelivr.net",
        "https://cdnjs.cloudflare.com",
        "https://ajax.googleapis.com",
        "https://fonts.googleapis.com",
        "https://fonts.gstatic.com",
    )
    TYPES = {"script", "stylesheet", "image", "font", "media"}

    def __init__(self, static_origins=None):
        self.static_origins = {
            origin(u) for u in (self.DEFAULT_STATIC if static_origins is None else static_origins)
        }

    async def fulfill_static(self, route):
        request = route.request
        if (
            origin(request.url) not in self.static_origins
            or request.method != "GET"
            or request.resource_type not in self.TYPES
        ):
            return False
        # A new client is deliberately used: browser cookies, Authorization, Referer,
        # query secrets, redirect cookies and Set-Cookie cannot cross this boundary.
        parsed = urlsplit(request.url)
        if (
            parsed.username
            or parsed.password
            or re.search(r"(?:token|auth|nonce|session|key|password)=", parsed.query, re.I)
        ):
            return False
        url = request.url
        try:
            async with httpx.AsyncClient(timeout=10, follow_redirects=False, trust_env=False) as client:
                for _ in range(4):
                    client.cookies.clear()
                    async with client.stream("GET", url, headers={"Accept": "*/*"}) as response:
                        if response.is_redirect:
                            from urllib.parse import urljoin

                            url = urljoin(url, response.headers.get("location", ""))
                            if origin(url) not in self.static_origins:
                                return False
                            continue
                        if response.status_code != 200:
                            return False
                        chunks = []
                        size = 0
                        async for chunk in response.aiter_bytes():
                            size += len(chunk)
                            if size > 3_000_000:
                                return False
                            chunks.append(chunk)
                        await route.fulfill(
                            status=200,
                            body=b"".join(chunks),
                            headers={
                                "content-type": response.headers.get(
                                    "content-type", "application/octet-stream"
                                ),
                                "access-control-allow-origin": "*",
                            },
                        )
                        return True
        except httpx.HTTPError:
            pass
        return False
