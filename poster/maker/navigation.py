"""Explicit URL glob semantics and secret-safe navigation diagnostics."""

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


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
                        r"pass|token|secret|code|auth|cookie|session|sig|key|credential|saml|ticket",
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
        return urlunsplit((parsed.scheme, host, path, query, "[hidden]" if parsed.fragment else ""))
    except (TypeError, ValueError):
        return "[protected URL]"
