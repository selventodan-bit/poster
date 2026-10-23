"""Versioned, multi-channel identification; identification never implies publication support."""

import re
from dataclasses import asdict, dataclass
from urllib.parse import urlsplit

from bs4 import BeautifulSoup


@dataclass(frozen=True)
class EngineAdapter:
    id: str
    revision: int
    layer: str
    generator: str
    dom: str
    assets: str
    routes: str
    api: str
    capabilities: tuple[str, ...] = ("identification",)
    tested_versions: tuple[str, ...] = ()


ADAPTERS = (
    EngineAdapter(
        "xenforo",
        2,
        "forum",
        r"xenforo",
        '[data-xf-init],html[data-app="public"]',
        r"/js/xf/|/styles/[^/]+/xenforo",
        r"/threads/|/forums/[^/]+/post-thread",
        r"/index.php\?[^ ]*inline-mod",
    ),
    EngineAdapter(
        "phpbb",
        2,
        "forum",
        r"phpbb",
        "#phpbb,.phpbb,.postbody",
        r"/styles/prosilver/|/assets/javascript/core.js",
        r"posting\.php|viewtopic\.php",
        r"app\.php/",
        ("identification", "form-discovery", "browser-publication", "http-workflow"),
        ("3.3.15 / prosilver / controlled SQLite installation",),
    ),
    EngineAdapter(
        "vbulletin",
        2,
        "forum",
        r"vbulletin",
        "#vbulletin_html,.b-post,.vb5",
        r"vbulletin-core|/clientscript/vbulletin",
        r"newthread\.php|/forum/",
        r"/ajax/api/",
    ),
    EngineAdapter(
        "mybb",
        2,
        "forum",
        r"mybb",
        "#mybb,.tborder .thead",
        r"mybb\.js|/jscripts/thread.js",
        r"newthread\.php|showthread\.php",
        r"xmlhttp\.php",
        ("identification", "form-discovery", "browser-publication", "http-workflow"),
        ("1.8.41 / bundled SCEditor / controlled SQLite, default and token-source plugin",),
    ),
    EngineAdapter(
        "discourse",
        2,
        "forum",
        r"discourse",
        "#discourse-application,[data-discourse]",
        r"discourse-application|discourse\.js",
        r"/t/[^/]+/\d+",
        r"/posts\.json|/session/current.json",
    ),
    EngineAdapter(
        "smf",
        2,
        "forum",
        r"simple machines|smf",
        "#smfheader,#topic_container,#smflogo",
        r"/Themes/[^/]+/scripts/(?:script|jquery.sceditor|(?:minified_)[a-f0-9]+)",
        r"index.php\?.*(?:topic|action=post)",
        r"index.php\?.*action=xmlhttp",
        ("identification", "form-discovery", "browser-publication", "http-workflow"),
        ("2.1.8 / default theme, textarea / controlled MariaDB 11.4",),
    ),
    EngineAdapter(
        "invision",
        2,
        "forum",
        r"invision",
        '[data-ipsApp],[data-controller^="core."]',
        r"/applications/core/|ips\.core",
        r"/topic/|/forums/forum/",
        r"/api/forums/",
    ),
    EngineAdapter(
        "flarum",
        2,
        "forum",
        r"flarum",
        "#flarum-loading,.DiscussionPage,.App-content",
        r"/assets/forum-[^/]+\.js|flarum\.core",
        r"/d/\d+",
        r"/api/discussions|/api/posts",
        ("identification", "form-discovery", "browser-publication", "http-workflow"),
        ("2.0.2 / English, Markdown / controlled SQLite",),
    ),
    EngineAdapter(
        "wpforo",
        2,
        "forum",
        r"wpforo",
        "#wpforo,#wpforo-wrap,.wpforo-forum",
        r"/wp-content/plugins/wpforo/",
        r"/community/|/participant/",
        r"wpforo|wpf_",
    ),
    EngineAdapter(
        "wordpress",
        2,
        "platform",
        r"wordpress",
        'body.wp-singular,body.wordpress,[rel="https://api.w.org/"]',
        r"/wp-content/|/wp-includes/",
        r"/wp-login.php|/wp-admin/",
        r"/wp-json/",
    ),
)
# Compatibility name for consumers that enumerate the registry.
SIGNATURES = ADAPTERS


@dataclass
class Technology:
    family: str
    layer: str
    version: str | None
    adapter_revision: int
    confidence: float
    evidence: list[str]
    capabilities: tuple[str, ...]
    tested_versions: tuple[str, ...]


class EngineDetector:
    def __init__(self, signatures=ADAPTERS):
        self.signatures = signatures

    def inspect(self, html: str, *, url="", requests=(), responses=(), behavior=()) -> list[Technology]:
        soup = BeautifulSoup(html, "html.parser")
        generators = " ".join(n.get("content", "") for n in soup.select('meta[name="generator" i]'))
        assets = " ".join(n.get("src", n.get("href", "")) for n in soup.select("script[src],link[href]"))
        routes = " ".join(
            [url] + [n.get("href", n.get("action", "")) for n in soup.select("a[href],form[action]")]
        )
        # Paths only: raw bodies, user content and secret query values are not detection evidence.
        api = " ".join(
            urlsplit(r.get("url", "")).path for r in requests if r.get("resource_type") in {"fetch", "xhr"}
        )
        found = []
        for adapter in self.signatures:
            evidence = []
            for channel, pattern, value in [
                ("generator", adapter.generator, generators),
                ("assets", adapter.assets, assets),
                ("routes", adapter.routes, routes),
                ("api", adapter.api, api),
            ]:
                if re.search(pattern, value, re.I):
                    evidence.append(channel)
            if soup.select_one(adapter.dom):
                evidence.append("dom")
            if adapter.id in behavior:
                evidence.append("behavior")
            if any(
                adapter.id in str(r.get("headers", {}).get("x-powered-by", "")).lower() for r in responses
            ):
                evidence.append("headers")
            if len(evidence) < 2:
                continue
            version = None
            if "generator" in evidence:
                match = re.search(
                    r"(?:" + adapter.generator + r")\s*([0-9]+(?:\.[0-9]+){0,3})", generators, re.I
                )
                if match:
                    version = match[1]
            found.append(
                Technology(
                    adapter.id,
                    adapter.layer,
                    version,
                    adapter.revision,
                    min(0.95, 0.3 * len(evidence)),
                    evidence,
                    adapter.capabilities,
                    adapter.tested_versions,
                )
            )
        return sorted(found, key=lambda t: (-t.confidence, t.layer != "forum", t.family))

    def detect(self, html: str) -> tuple[str, float, list[str]]:
        found = self.inspect(html)
        if not found:
            return "unknown", 0, []
        first = found[0]
        return first.family, first.confidence, [f"{first.family}:{e}" for e in first.evidence]

    def report(self, html, **kwargs):
        return [asdict(t) for t in self.inspect(html, **kwargs)]
