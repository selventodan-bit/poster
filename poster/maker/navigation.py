"""Explicit URL glob semantics and secret-safe navigation diagnostics."""

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

SENSITIVE_KEY = r"pass|token|secret|code|auth|cookie|session|sig|key|credential|saml|ticket"


def sensitive_url(url):
    try:
        parsed = urlsplit(url)
        return any(re.search(SENSITIVE_KEY, key, re.I) for key, _ in parse_qsl(parsed.query)) or bool(
            re.search(r"(?:^|[?&#])(?:[^=]*(?:" + SENSITIVE_KEY + r")[^=]*)=", parsed.fragment, re.I)
        )
    except ValueError:
        return True


def matches(pattern, url):
    # ** spans path separators, * matches a segment; other characters are literal.
    expression = "".join(
        ".*" if part == "**" else "[^/]*" if part == "*" else re.escape(part)
        for part in re.split(r"(\*\*|\*)", pattern)
    )
    return re.fullmatch(expression, url) is not None


def safe_url(url):
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        if parsed.port:
            host += ":" + str(parsed.port)
        path = "/".join(
            "[protected]" if re.fullmatch(r"[A-Za-z0-9_.~-]{24,}", item) else item
            for item in parsed.path.split("/")
        )
        query = urlencode(
            [
                (
                    name,
                    "[hidden]"
                    if re.search(
                        SENSITIVE_KEY,
                        name,
                        re.I,
                    )
                    or len(value) > 128
                    or re.fullmatch(r"[A-Za-z0-9_.~-]{24,}", value)
                    else value,
                )
                for name, value in parse_qsl(parsed.query, keep_blank_values=True)
            ]
        )
        fragment = (
            "[hidden]"
            if sensitive_url("http://local/#" + parsed.fragment) or len(parsed.fragment) > 128
            else parsed.fragment
        )
        return urlunsplit((parsed.scheme, host, path, query, fragment))
    except (TypeError, ValueError):
        return "[protected URL]"
