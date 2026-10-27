"""Private network profiles; public graphs contain only a logical binding name."""

import asyncio
from dataclasses import dataclass, field
import ipaddress
import logging
import os
import re
import socket
import ssl
from urllib.parse import urlsplit
import uuid

import httpx

from poster.cloud.auth import SecretStore
from poster.maker.model import GraphError

ERRORS = {
    "NETWORK_INVALID": "Проверьте тип, адрес и порт сетевого профиля.",
    "NETWORK_MISSING": "Настройте приватный сетевой профиль в этой среде.",
    "NETWORK_OVERRIDE": "Проект запрещает замену сетевого профиля при запуске.",
    "NETWORK_UNSUPPORTED": "Выбранный браузер или HTTP backend не поддерживает эту конфигурацию proxy.",
    "NETWORK_TLS": "Не подтверждён TLS сертификат. Проверьте доверенный CA, имя и срок действия.",
    "NETWORK_DNS": "Не удалось разрешить DNS имя подключения.",
    "NETWORK_AUTH": "Proxy отклонил авторизацию. Исправьте приватные credentials.",
    "NETWORK_CONNECT": "Соединение с proxy или сервером недоступно.",
    "NETWORK_TIMEOUT": "Истёк срок проверки сетевого подключения.",
}


def policy(value):
    if value is None:
        return None
    if (
        not isinstance(value, dict)
        or set(value) != {"binding", "allow_override"}
        or not isinstance(value["binding"], str)
        or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", value["binding"])
        or type(value["allow_override"]) is not bool
    ):
        raise GraphError("NETWORK_INVALID")
    return dict(value)


@dataclass(frozen=True, repr=False)
class Network:
    kind: str = "direct"
    host: str = ""
    port: int = 0
    username: str = field(default="", repr=False)
    password: str = field(default="", repr=False)
    ca: str = field(default="", repr=False)

    def __repr__(self):
        return f"Network(kind={self.kind!r}, private=True)"

    @classmethod
    def parse(cls, value=None):
        if isinstance(value, cls):
            return value
        if value is None:
            return cls()
        try:
            if not isinstance(value, dict) or set(value) - set(cls.__dataclass_fields__):
                raise ValueError()
            result = cls(**value)
            if result.kind not in {"direct", "http", "https", "socks5"}:
                raise ValueError()
            if not all(
                isinstance(x, str) for x in (result.host, result.username, result.password, result.ca)
            ):
                raise ValueError()
            if any(len(x) > 4096 or any(ord(c) < 32 for c in x) for x in (result.username, result.password)):
                raise ValueError()
            if result.kind == "direct":
                if result.host or result.port or result.username or result.password or result.ca:
                    raise ValueError()
            else:
                if (
                    not result.host
                    or len(result.host) > 253
                    or type(result.port) is not int
                    or not 1 <= result.port <= 65535
                    or len(result.ca) > 100_000
                ):
                    raise ValueError()
                try:
                    ipaddress.ip_address(result.host)
                except ValueError:
                    if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", result.host):
                        raise ValueError() from None
                if result.password and not result.username:
                    raise ValueError()
                if result.ca and result.kind != "https":
                    raise ValueError()
            return result
        except (TypeError, ValueError) as exc:
            raise GraphError("NETWORK_INVALID") from exc

    def private(self):
        return {key: getattr(self, key) for key in self.__dataclass_fields__}

    def check(self, *, browser=False, provider="chromium"):
        if not browser or self.kind == "direct":
            return
        if provider != "chromium" or self.ca or self.kind == "socks5" and self.username:
            raise GraphError("NETWORK_UNSUPPORTED")

    @property
    def server(self):
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{self.kind}://{host}:{self.port}"

    def chromium(self):
        self.check(browser=True)
        if self.kind == "direct":
            return None
        result = dict(server=self.server, bypass="<-loopback>")
        if self.username:
            result.update(username=self.username, password=self.password)
        return result

    def transport(self):
        # Explicit config and retries=0: neither environment proxy nor write retries.
        for name in ("httpx", "httpcore", "socksio"):
            logging.getLogger(name).setLevel(logging.WARNING)
        proxy = None
        if self.kind != "direct":
            context = ssl.create_default_context()
            if self.ca:
                context.load_verify_locations(cadata=self.ca)
            proxy = httpx.Proxy(
                self.server,
                auth=(self.username, self.password) if self.username else None,
                ssl_context=context if self.kind == "https" else None,
            )
        try:
            return httpx.AsyncHTTPTransport(proxy=proxy, retries=0, trust_env=False)
        except ImportError as exc:
            raise GraphError("NETWORK_UNSUPPORTED") from exc


def error_code(exc):
    chain, current = [], exc
    for _ in range(12):
        if current is None or current in chain:
            break
        chain.append(current)
        current = current.__cause__ or current.__context__
    if any(isinstance(e, (TimeoutError, httpx.TimeoutException)) for e in chain):
        return "NETWORK_TIMEOUT"
    if any(isinstance(e, ssl.SSLError) or "CERTIFICATE_VERIFY_FAILED" in str(e) for e in chain):
        return "NETWORK_TLS"
    if any(isinstance(e, socket.gaierror) for e in chain):
        return "NETWORK_DNS"
    if any(
        isinstance(e, httpx.ProxyError)
        and any(word in str(e).lower() for word in ("407", "auth", "username/password"))
        for e in chain
    ):
        return "NETWORK_AUTH"
    return "NETWORK_CONNECT"


async def probe(config, url, *, timeout=10):
    config = Network.parse(config)
    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise GraphError("NETWORK_INVALID")
    try:
        async with asyncio.timeout(min(30, max(1, timeout))):
            async with httpx.AsyncClient(transport=config.transport(), trust_env=False) as client:
                async with client.stream("GET", url, timeout=timeout) as response:
                    if response.status_code == 407:
                        raise GraphError("NETWORK_AUTH")
                    # No response payload, credentials or operator URL in diagnostics.
                    return dict(verified=True, code="OK", status=response.status_code, kind=config.kind)
    except GraphError:
        raise
    except (httpx.HTTPError, OSError, TimeoutError) as exc:
        raise GraphError(error_code(exc)) from None


class NetworkProfiles:
    def __init__(self, root=None):
        self.secrets = SecretStore(root or os.environ.get("POSTER_NETWORK_HOME"))

    def rows(self):
        data = self.secrets.get("network-profiles") or {}
        return [dict(id=key, name=value["name"], kind=value["config"]["kind"]) for key, value in data.items()]

    def put(self, name, config, profile_id=None):
        if not isinstance(name, str) or not name.strip() or len(name) > 100:
            raise GraphError("NETWORK_INVALID")
        config = Network.parse(config)
        profile_id = profile_id or uuid.uuid4().hex
        policy(dict(binding=profile_id, allow_override=False))
        with self.secrets.lock:
            data = self.secrets.get("network-profiles") or {}
            data[profile_id] = dict(name=name.strip(), config=config.private())
            self.secrets.put("network-profiles", data)
        return profile_id

    def delete(self, profile_id):
        with self.secrets.lock:
            data = self.secrets.get("network-profiles") or {}
            data.pop(profile_id, None)
            self.secrets.put("network-profiles", data)

    def get(self, profile_id):
        data = self.secrets.get("network-profiles") or {}
        if profile_id not in data:
            raise GraphError("NETWORK_MISSING")
        return Network.parse(data[profile_id]["config"])

    def resolve(self, requested=None, *, override=None):
        requested = policy(requested)
        if override is not None and requested and not requested["allow_override"]:
            raise GraphError("NETWORK_OVERRIDE")
        return self.get(override or requested["binding"]) if override or requested else Network()
