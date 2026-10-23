"""Browser resource and mutation policies. Consent grants a bounded content-linked transaction."""

import hashlib
import json
import re
import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit, parse_qsl, unquote

import httpx

from .network import decode, request_values, response_values, text


def origin(url):
    parsed = urlsplit(url)
    return parsed.scheme, parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)


def dangerous_url(url):
    return bool(
        re.search(
            r"(?:[/=?&;_-])(?:delete|logout|unsubscribe|vote|remove|settings|preferences|profile)(?:[/=?&;_-]|$)",
            url,
            re.I,
        )
    )


class SubmissionScope:
    def __init__(
        self, url, capture, *, max_writes=6, templates=(), submitter=None, action_url=None, interaction=None
    ):
        self.origin = origin(url)
        self.capture = capture
        self.max_writes = max_writes
        self.sent = set()
        self.allowed_ids = set()
        self.templates = templates
        self.submitter, self.action_url = submitter, action_url
        self.interaction = interaction

    def allows(self, request, *, native_authorized=False):
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
            encoding, payload = decode({"content_type": request.headers.get("content-type", ""), "body": raw})
        except (ValueError, UnicodeError, LookupError):
            return False
        if not self.sent:
            purpose = urlsplit(request.url).path + "?" + urlsplit(request.url).query
            if isinstance(payload, dict):
                purpose += " " + " ".join(
                    str(v)
                    for k, v in payload.items()
                    if k.lower() in {"action", "mode", "operation", "op", "do"}
                )
            if re.search(
                r"(?:^|[/=?&;_\s-])(?:autosave|preview|upload|attachment|edit|delete|vote|settings)(?:$|[/=?&;_\s-])",
                purpose,
                re.I,
            ):
                return False
            if re.search(r"(?:^|[/=?&;_\s-])drafts?(?:$|[/=?&;_\s-])", purpose, re.I):
                from .network import leaves

                # A title-only draft can start a multi-step topic transaction;
                # storing a full post as a draft needs a previously proven recipe.
                if any(
                    isinstance(v, str) and text(v) == text(self.capture.body_marker)
                    for _, v in leaves(payload)
                ):
                    from .transfer import shape

                    roles = sorted(
                        role
                        for role, marker in (
                            ("title", self.capture.title_marker),
                            ("body", self.capture.body_marker),
                        )
                        if role in self.capture.content_roles
                        and any(
                            isinstance(v, str)
                            and (marker in v if role == "title" else text(v) == text(marker))
                            for _, v in leaves(payload)
                        )
                    )
                    if not any(
                        t.operations
                        and t.operations[0].route == "draft"
                        and t.operations[0].method == request.method
                        and t.operations[0].encoding == encoding
                        and t.operations[0].roles == roles
                        and t.operations[0].shape == shape(payload)
                        for t in self.templates
                    ):
                        return False
            signed = self.interaction and request.headers.get("x-poster-interaction") == self.interaction
            # A selected click may GET a fresh token before its first POST. That
            # read and consumed high-entropy value establish causality only for
            # the current explicit form endpoint; timing/content alone do not.
            fresh_read = False
            if self.interaction and self.action_url == urlsplit(request.url)._replace(fragment="").geturl():
                reads = {
                    r["id"]
                    for r in self.capture.requests
                    if r.get("interaction") == self.interaction and r["method"] == "GET"
                }
                for response in self.capture.responses:
                    if response.get("request_id") not in reads or response.get("status", 500) >= 400:
                        continue
                    fresh_read |= any(
                        isinstance(value, str) and len(value) >= 16 and str(value) == str(used)
                        for _, _, value in response_values(response)
                        for _, _, used in request_values(
                            {"payload": payload, "url": request.url, "headers": request.headers}
                        )
                    )
            if (
                self.interaction
                and not signed
                and not fresh_read
                and not (native_authorized and getattr(request, "resource_type", None) == "document")
            ):
                return False
            if self.submitter and (
                not isinstance(payload, dict) or payload.get(self.submitter[0]) != self.submitter[1]
            ):
                return False
            if (
                self.action_url
                and not signed
                and urlsplit(request.url)._replace(fragment="").geturl() != self.action_url
            ):
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
            and (
                ("title" in self.capture.content_roles and self.capture.title_marker in v)
                or text(self.capture.body_marker) == text(v)
            )
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
        urls = self.DEFAULT_STATIC if static_origins is None else static_origins
        for url in urls:
            parsed = urlsplit(url)
            if (
                parsed.scheme != "https"
                or parsed.username
                or parsed.password
                or parsed.path not in {"", "/"}
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError("Static resources require exact HTTPS origins")
        self.static_origins = {origin(u) for u in urls}
        self.last_reason = None

    async def public_addresses(self, hostname, port):
        records = await asyncio.get_running_loop().getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
        addresses = list(dict.fromkeys(r[4][0] for r in records))
        parsed_addresses = [ipaddress.ip_address(a) for a in addresses]
        if not addresses or any(not a.is_global or a.is_multicast or a.is_reserved for a in parsed_addresses):
            raise ValueError("Static origin resolves to a private or reserved address")
        return addresses

    def safe_url(self, url):
        parsed = urlsplit(url)
        queries = parse_qsl(parsed.query, keep_blank_values=True)
        defaults = {origin(u) for u in self.DEFAULT_STATIC}
        # Custom origins are intentionally path-only: opaque query parameters
        # cannot be proven free of secrets by their names alone.
        if queries and origin(url) not in defaults:
            return False
        if any(
            k not in {"v", "ver", "version", "family", "display", "subset", "icon_names", "module"}
            for k, _ in queries
        ):
            return False
        return (
            origin(url) in self.static_origins
            and not parsed.username
            and not parsed.password
            and not any(
                re.search(
                    r"token|auth|nonce|session|key|password|secret|credential|signature", unquote(k), re.I
                )
                for k, _ in parse_qsl(parsed.query)
            )
        )

    async def fulfill_static(self, route):
        self.last_reason = "STATIC_ORIGIN_UNAPPROVED"
        request = route.request
        if (
            origin(request.url) not in self.static_origins
            or request.method != "GET"
            or request.resource_type not in self.TYPES
        ):
            return False
        # A new client is deliberately used: browser cookies, Authorization, Referer,
        # query secrets, redirect cookies and Set-Cookie cannot cross this boundary.
        if not self.safe_url(request.url):
            self.last_reason = "STATIC_ADDRESS_REJECTED"
            return False
        url = request.url
        try:
            async with httpx.AsyncClient(timeout=10, follow_redirects=False, trust_env=False) as client:
                for _ in range(4):
                    if not self.safe_url(url):
                        self.last_reason = "STATIC_REDIRECT_REJECTED"
                        return False
                    parsed = urlsplit(url)
                    addresses = await self.public_addresses(parsed.hostname, parsed.port or 443)
                    # Pin the validated address; re-resolving in httpx would permit DNS rebinding.
                    pinned = httpx.URL(url).copy_with(host=addresses[0])
                    client.cookies.clear()
                    async with client.stream(
                        "GET",
                        pinned,
                        headers={"Accept": "*/*", "Host": parsed.netloc},
                        extensions={"sni_hostname": parsed.hostname},
                    ) as response:
                        if response.is_redirect:
                            from urllib.parse import urljoin

                            url = urljoin(url, response.headers.get("location", ""))
                            if origin(url) not in self.static_origins:
                                self.last_reason = "STATIC_REDIRECT_REJECTED"
                                return False
                            continue
                        if response.status_code != 200:
                            self.last_reason = "STATIC_HTTP_FAILED"
                            return False
                        chunks = []
                        size = 0
                        async for chunk in response.aiter_bytes():
                            size += len(chunk)
                            if size > 3_000_000:
                                self.last_reason = "STATIC_SIZE_LIMIT"
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
                        self.last_reason = None
                        return True
        except ValueError:
            self.last_reason = "STATIC_ADDRESS_REJECTED"
        except (httpx.HTTPError, OSError):
            self.last_reason = "STATIC_NETWORK_FAILED"
        return False
