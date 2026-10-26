"""Read official public provider docs; no account access or cloud provisioning."""

import hashlib
import html
import json
from pathlib import Path
import re
import urllib.request

SOURCES = {
    "drive": "https://developers.google.com/workspace/drive/api/guides/about-sdk",
    "drive_create": "https://developers.google.com/workspace/drive/api/guides/create-file",
    "drive_errors": "https://developers.google.com/workspace/drive/api/guides/handle-errors",
    "drive_fields": "https://developers.google.com/workspace/drive/api/reference/rest/v3/files",
    "oauth": "https://developers.google.com/identity/protocols/oauth2/native-app",
    "colab_faq": "https://research.google.com/colaboratory/faq.html",
    "molab": "https://molab.marimo.io/blog/drive-storage",
    "marimo": "https://docs.marimo.io/",
}
KEYWORDS = (
    "etag",
    "if-match",
    "precondition",
    "generated id",
    "pre-generated",
    "412",
    "PKCE",
    "installed app",
    "loopback",
    "distributed",
    "remote",
    "compute unit",
    "free of charge",
    "gdrive",
    "token",
    "auth",
    "concurr",
    "session",
    "quota",
    "Windows",
    "MCP",
)


def main():
    target = Path(".poster/stage5c-provider-docs")
    target.mkdir(parents=True, exist_ok=True)
    records = {}
    for name, url in SOURCES.items():
        request = urllib.request.Request(url, headers={"User-Agent": "Poster provider compatibility audit"})
        with urllib.request.urlopen(request, timeout=45) as response:
            raw = response.read(3_000_000)
            resolved = response.url
        page = raw.decode("utf-8", errors="replace")
        article = re.search(r"<main\b[^>]*>(.*?)</main>", page, flags=re.S | re.I)
        if article:
            page = article.group(1)
        page = re.sub(r"<(script|style)\b.*?</\1>", "", page, flags=re.S | re.I)
        page = re.sub(r"<(nav|aside|header|footer)\b.*?</\1>", "", page, flags=re.S | re.I)
        text = html.unescape(re.sub(r"<[^>]+>", "\n", page))
        lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
        text = "\n".join(line for line in lines if line)
        (target / (name + ".txt")).write_text(text, encoding="utf-8")
        selected = []
        for i, line in enumerate(lines):
            if any(word.lower() in line.lower() for word in KEYWORDS):
                selected.extend(lines[max(0, i - 1) : i + 3])
        record = dict(url=resolved, sha256=hashlib.sha256(raw).hexdigest(), bytes=len(raw))
        records[name] = record
        excerpt = "\n".join(dict.fromkeys(selected))[:3900]
        while len(json.dumps(dict(**record, excerpts=excerpt), ensure_ascii=True)) > 4500:
            excerpt = excerpt[: len(excerpt) * 3 // 4]
        print(
            "::notice title=Stage 5C official documentation "
            + name
            + "::"
            + json.dumps(dict(**record, excerpts=excerpt), ensure_ascii=True)
        )
    (target / "sources.json").write_text(json.dumps(records, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
