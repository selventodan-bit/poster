"""Read JSON configuration literals in HTML, without executing JavaScript.

WordPress plugins and other CMSes commonly publish an AJAX nonce in an inline
JSON object. Only actual JSON, bounded scripts and unique scalar sources qualify.
Arbitrary JS expressions, functions and computed/encrypted values are refused.
"""

import json
import re

from bs4 import BeautifulSoup

from .network import leaves


def token_slots(body):
    soup = BeautifulSoup(body[:2_000_000], "html.parser")
    found = []
    for script in soup.select("script:not([src])")[:100]:
        raw = script.string or script.get_text()
        if len(raw) > 200_000:
            continue
        roots = []
        if script.get("type") == "application/json":
            roots = [0]
        else:
            roots = [
                m.end()
                for m in re.finditer(
                    r"(?:\b(?:var|let|const)\s+[A-Za-z_$][\w$]*|\bwindow\.[A-Za-z_$][\w$]*)\s*=\s*(?=\{)", raw
                )
            ]
        for start in roots[:50]:
            try:
                data, _ = json.JSONDecoder().raw_decode(raw[start:])
            except ValueError:
                continue
            for path, value in leaves(data):
                key = path.rsplit(".", 1)[-1]
                if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,79}", key):
                    continue
                if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,100}", value):
                    continue
                # A local key and a generic value class: no captured token is
                # written into the executable project's extraction expression.
                pattern = '"' + re.escape(key) + r'"\s*:\s*"([A-Za-z0-9_-]{8,100})"'
                if len(re.findall(pattern, body)) == 1:
                    found.append((pattern, value))
    return list(dict.fromkeys(found))
