"""Universal HTTP actions using the existing httpx stack, never a browser."""

import asyncio
import json
import logging
import re
from typing import Any, Literal
from urllib.parse import urljoin, urlsplit

import httpx
from pydantic import Field

from ..network import bounded_body, ResponseTooLarge
from .actions import Action, Empty
from .action_fields import properties
from .model import DataModel, GraphError, secret_refs
from .resources import MAX_DATA, MAX_FILE
from .navigation import sensitive_url
from .data_actions import load_json

SENSITIVE = re.compile(r"authorization|cookie|pass|secret|token|api.?key|session", re.I)


class Request(DataModel):
    url: str = Field(default="https://example.org", description="HTTP(S) URL; допускается {{переменная}}")
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"] = "GET"
    query: dict[str, Any] = Field(default_factory=dict, description="Query-параметры: JSON-объект")
    headers: dict[str, str] = Field(
        default_factory=dict, description="Заголовки: JSON-объект; секреты защищайте"
    )
    cookies: dict[str, str] = Field(default_factory=dict, description="Cookies только этого запроса")
    body_type: Literal["none", "json", "form", "multipart", "text"] = "none"
    body: Any = Field(default=None, description="JSON, поля формы либо текст; файлы задаются отдельно")
    files: dict[str, str] = Field(
        default_factory=dict, description="Multipart: имя поля → путь внутри рабочего каталога"
    )
    timeout_ms: int = Field(default=30000, ge=10, le=120000, description="Общий таймаут запроса, мс")
    follow_redirects: bool = True
    expected_status: list[int] = Field(
        default_factory=lambda: list(range(200, 300)),
        max_length=600,
        description="Допустимые статусы; [] разрешает любой статус",
    )
    response_type: Literal["text", "json", "auto", "binary"] = "auto"
    max_response_bytes: int = Field(default=MAX_DATA // 2, ge=1, le=MAX_FILE)
    download_path: str = Field(
        default="", description="Путь для бинарного ответа; относительно рабочего каталога"
    )
    overwrite: bool = False
    protect_response: bool = Field(
        default=True, description="Защитить тело ответа; отключайте для доверенных открытых данных"
    )


class Header(DataModel):
    name: str = Field(default="User-Agent", min_length=1, max_length=200)
    value: str = Field(default="Poster/3", max_length=8192)


class Cookie(DataModel):
    name: str = Field(default="session", min_length=1, max_length=256)
    value: str = Field(default="", max_length=4096)
    url: str = "https://example.org/"
    path: str = "/"
    secure: bool = True


def valid_url(url):
    p = urlsplit(url)
    if p.scheme not in {"http", "https"} or not p.hostname or p.username or p.password:
        raise GraphError("URL_INVALID")
    return p


def origin(url):
    p = valid_url(url)
    return p.scheme, p.hostname, p.port or (443 if p.scheme == "https" else 80)


class HTTPState:
    def __init__(self, network=None):
        from poster.portability.network import Network

        self.client = httpx.AsyncClient(
            trust_env=False, follow_redirects=False, transport=Network.parse(network).transport()
        )
        self.tainted = False
        self.headers = {}
        for name in ("httpx", "httpcore"):
            logging.getLogger(name).setLevel(logging.WARNING)

    async def close(self):
        await self.client.aclose()


def session(ctx):
    if ctx.http is None:
        network = getattr(ctx, "network", None)
        ctx.http = HTTPState(network) if network and network.kind != "direct" else HTTPState()
    return ctx.http


def protect_values(vault, value):
    if isinstance(value, str) and sensitive_url(value):
        return vault.put_value(value)
    if isinstance(value, dict):
        return {
            k: vault.put_value(v)
            if SENSITIVE.search(k) and v not in (None, "", {}, [])
            else protect_values(vault, v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [protect_values(vault, v) for v in value]
    return value


def protect(ctx, value):
    return protect_values(ctx.vault, value)


def has_sensitive(value):
    if isinstance(value, dict):
        return any(SENSITIVE.search(k) or has_sensitive(v) for k, v in value.items())
    return isinstance(value, list) and any(has_sensitive(v) for v in value)


def protect_graph(graph, vault):
    """Protect literal HTTP credentials before serializing a project."""
    from .variables import has_reference

    def fields(value):
        if isinstance(value, str) and value.startswith(("http://", "https://")) and not has_reference(value):
            parsed = urlsplit(value)
            if sensitive_url(value) or parsed.username or parsed.password:
                return vault.put_value(value)
        if isinstance(value, dict):
            if set(value) == {"secret_ref"}:
                return value
            return {
                k: vault.put_value(v)
                if SENSITIVE.search(k)
                and v not in (None, "", {}, [])
                and not secret_refs(v)
                and not has_reference(v)
                else fields(v)
                for k, v in value.items()
            }
        return [fields(v) for v in value] if isinstance(value, list) else value

    for node in graph.nodes:
        if not node.type.startswith("HTTP."):
            continue
        node.params = fields(node.params)
        if (
            node.type in {"HTTP.SetCookie", "HTTP.SetHeader"}
            and (node.type == "HTTP.SetCookie" or SENSITIVE.search(node.params.get("name", "")))
            and "value" in node.params
        ):
            value = node.params["value"]
            if not secret_refs(value) and not has_reference(value):
                node.params["value"] = vault.put_value(value)


async def request(ctx, p, method=None, download=False):
    state = session(ctx)
    method, url = method or p.method, p.url
    valid_url(url)
    if download and not p.download_path:
        raise GraphError("FILE_PATH_INVALID")
    headers = {**state.headers, **p.headers}
    if len(headers) > 100 or sum(len(k) + len(v) for k, v in headers.items()) > 65536:
        raise GraphError("HTTP_RESPONSE_LIMIT")
    sensitive = (
        ctx.sensitive_action
        or state.tainted
        or any(SENSITIVE.search(k) for k in headers)
        or bool(p.cookies)
        or bool(state.client.cookies.jar)
        or has_sensitive(p.body)
        or has_sensitive(p.query)
        or sensitive_url(p.url)
    )
    kwargs = {"params": p.query}
    if p.cookies:
        # Request-local cookies are never installed as unscoped session cookies.
        if "cookie" in {k.lower() for k in headers}:
            raise GraphError("PARAMETERS_INVALID")
        req = httpx.Request("GET", url)
        httpx.Cookies(p.cookies).set_cookie_header(req)
        headers["Cookie"] = req.headers.get("cookie", "")
    if p.body_type == "json":
        kwargs["json"] = p.body
    elif p.body_type in {"form", "multipart"}:
        if not isinstance(p.body, dict):
            raise GraphError("HTTP_BODY_INVALID")
        if p.body_type == "form":
            kwargs["data"] = p.body
        else:
            kwargs["files"] = {k: (None, str(v)) for k, v in p.body.items()}
            total = 0
            for name, path in p.files.items():
                raw = ctx.files.read(path)
                total += len(raw)
                if total > MAX_FILE:
                    raise GraphError("FILE_LIMIT")
                kwargs["files"][name] = (ctx.files.path(path).name, raw)
    elif p.body_type == "text":
        if not isinstance(p.body, str):
            raise GraphError("HTTP_BODY_INVALID")
        kwargs["content"] = p.body.encode("utf-8")
    if p.body_type != "multipart" and p.files:
        raise GraphError("HTTP_BODY_INVALID")
    if len(json.dumps(p.body, ensure_ascii=False).encode()) > MAX_DATA:
        raise GraphError("DATA_LIMIT")
    try:
        async with asyncio.timeout(p.timeout_ms / 1000):
            for redirect in range(11):
                async with state.client.stream(
                    method, url, headers=headers, timeout=p.timeout_ms / 1000, **kwargs
                ) as r:
                    raw = await bounded_body(r, p.max_response_bytes)
                    status, response_headers = r.status_code, dict(r.headers)
                    if status == 407 and ctx.network.kind != "direct":
                        raise GraphError("NETWORK_AUTH")
                    if (
                        sum(len(k) + len(v) for k, v in response_headers.items()) > 65536
                        or len(state.client.cookies.jar) > 1000
                    ):
                        state.client.cookies.clear()
                        raise GraphError("HTTP_RESPONSE_LIMIT")
                    encoding = r.encoding or "utf-8"
                    cookies = {c.name: ctx.vault.put_value(c.value) for c in r.cookies.jar}
                if 300 <= status < 400 and "location" in response_headers and p.follow_redirects:
                    if redirect == 10:
                        raise GraphError("HTTP_REDIRECT_LIMIT")
                    target = urljoin(url, response_headers["location"])
                    if urlsplit(url).scheme == "https" and urlsplit(target).scheme != "https":
                        raise GraphError("HTTP_REDIRECT_UNSAFE")
                    if status in {307, 308} and method not in {"GET", "HEAD"}:
                        raise GraphError("HTTP_WRITE_UNKNOWN")
                    if origin(target) != origin(url):
                        # Do not forward any caller/session custom headers across origins.
                        headers = {}
                    if status == 303 or (status in {301, 302} and method == "POST"):
                        method = "GET"
                        kwargs = {}
                    kwargs.pop("params", None)
                    url = target
                    continue
                break
    except ResponseTooLarge as exc:
        raise GraphError("HTTP_RESPONSE_LIMIT") from exc
    except (TimeoutError, httpx.TimeoutException) as exc:
        raise GraphError(
            "HTTP_TIMEOUT",
            details={
                "write_outcome_unknown": method not in {"GET", "HEAD"}
                and not isinstance(exc, (httpx.ConnectTimeout, httpx.PoolTimeout)),
                "phase": "connect"
                if isinstance(exc, httpx.ConnectTimeout)
                else "pool"
                if isinstance(exc, httpx.PoolTimeout)
                else "read"
                if isinstance(exc, httpx.ReadTimeout)
                else "write"
                if isinstance(exc, httpx.WriteTimeout)
                else "total",
                "retried": False,
            },
        ) from exc
    except httpx.ConnectError as exc:
        if ctx.network.kind != "direct":
            from poster.portability.network import error_code

            raise GraphError(error_code(exc)) from None
        raise GraphError("HTTP_CONNECT") from exc
    except httpx.HTTPError as exc:
        if isinstance(exc, httpx.ProxyError) and ctx.network.kind != "direct":
            from poster.portability.network import error_code

            raise GraphError(error_code(exc)) from None
        raise GraphError("HTTP_WRITE_UNKNOWN" if method not in {"GET", "HEAD"} else "HTTP_NETWORK") from exc
    if p.expected_status and status not in p.expected_status:
        raise GraphError("HTTP_STATUS", details={"status": status, "retried": False})
    state.tainted |= sensitive or bool(cookies)
    ctx.sensitive_action |= sensitive or p.protect_response
    result = {
        "status": status,
        "headers": protect(ctx, response_headers),
        "cookies": cookies,
        "bytes": len(raw),
        "text": None,
        "json": None,
    }
    if p.download_path:
        result["path"] = ctx.files.write(p.download_path, raw, overwrite=p.overwrite)
    if p.response_type != "binary" and not download:
        try:
            result["text"] = raw.decode(encoding)
            is_json = p.response_type == "json" or (
                p.response_type == "auto" and "json" in response_headers.get("content-type", "")
            )
            if is_json and raw and method != "HEAD":
                result["json"] = protect(ctx, load_json(result["text"]))
            elif raw and "json" in response_headers.get("content-type", "") and method != "HEAD":
                try:
                    ctx.sensitive_action |= has_sensitive(json.loads(result["text"]))
                except ValueError:
                    pass  # text mode does not require valid JSON
        except (ValueError, LookupError, UnicodeError, RecursionError) as exc:
            raise GraphError("HTTP_DECODE") from exc
    ctx.sensitive_action |= bool(secret_refs(result))
    return "success", result


async def set_header(ctx, p):
    if p.name.lower() in {"host", "content-length", "cookie"}:
        raise GraphError("PARAMETERS_INVALID")
    state = session(ctx)
    state.headers[p.name] = p.value
    if len(state.headers) > 100 or sum(len(k) + len(v) for k, v in state.headers.items()) > 65536:
        state.headers.pop(p.name, None)
        raise GraphError("DATA_LIMIT")
    state.tainted |= ctx.sensitive_action or bool(SENSITIVE.search(p.name))
    return "success", {"updated": True}


async def set_cookie(ctx, p):
    from http.cookiejar import Cookie as JarCookie

    parsed = valid_url(p.url)
    state = session(ctx)
    state.client.cookies.jar.set_cookie(
        JarCookie(
            0,
            p.name,
            p.value,
            None,
            False,
            parsed.hostname,
            False,
            False,
            p.path,
            True,
            p.secure,
            None,
            True,
            None,
            None,
            {},
            False,
        )
    )
    state.tainted = True
    if len(state.client.cookies.jar) > 1000:
        state.client.cookies.clear()
        raise GraphError("DATA_LIMIT")
    return "success", {"updated": True}


async def clear_cookies(ctx, p):
    session(ctx).client.cookies.clear()
    return "success", {"cleared": True}


def register_http_actions(registry):
    from functools import partial

    for name, label, execute, model in (
        ("Request", "Запрос", request, Request),
        ("Get", "GET", partial(request, method="GET"), Request),
        ("Post", "POST", partial(request, method="POST"), Request),
        ("Download", "Скачать файл", partial(request, method="GET", download=True), Request),
        ("SetHeader", "Задать заголовок", set_header, Header),
        ("SetCookie", "Задать cookie", set_cookie, Cookie),
        ("ClearCookies", "Очистить cookies", clear_cookies, Empty),
    ):
        fields = properties(
            model, paths=("download_path",), secrets=("url", "query", "headers", "cookies", "body", "value")
        )
        if name in {"Get", "Post", "Download"}:
            # The alias fixes its verb. Keep accepting old serialized method
            # fields, but do not offer an ineffective method selector in the UI.
            fields = tuple(prop for prop in fields if prop.name != "method")
        registry.register(
            Action(
                "HTTP." + name,
                "HTTP · " + label,
                model,
                execute,
                fields,
                reveal_parameters=True,
            )
        )
