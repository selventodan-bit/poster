"""Engine identification is evidence, not a claim that an engine's publishing API is supported."""

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class EngineSignature:
    id: str
    version: int
    patterns: tuple[str, ...]


SIGNATURES = (
    EngineSignature("xenforo", 1, (r'content=["\'][^"\']*xenforo', r"/js/xf/", r"data-xf-init")),
    EngineSignature("phpbb", 1, (r'content=["\'][^"\']*phpbb', r"/styles/prosilver/", r"posting\.php")),
    EngineSignature("vbulletin", 1, (r'content=["\'][^"\']*vbulletin', r"vbulletin-core", r"/vb/")),
    EngineSignature("mybb", 1, (r'content=["\'][^"\']*mybb', r"mybb\.js", r"newthread\.php")),
    EngineSignature(
        "discourse", 1, (r'content=["\'][^"\']*discourse', r"discourse-application", r"data-discourse")
    ),
    EngineSignature("smf", 1, (r'content=["\'][^"\']*simple machines', r"smf_scripturl", r"smf_theme_url")),
    EngineSignature("invision", 1, (r'content=["\'][^"\']*invision', r"ips\.controller", r"data-ips")),
    EngineSignature("flarum", 1, (r'content=["\'][^"\']*flarum', r"flarum\.core", r"flarum-app")),
    EngineSignature("wpforo", 1, (r"wpforo", r"wpforo-forum", r"/wp-content/plugins/wpforo")),
    EngineSignature("wordpress", 1, (r'content=["\'][^"\']*wordpress', r"/wp-content/", r"/wp-includes/")),
)


class EngineDetector:
    def __init__(self, signatures=SIGNATURES):
        self.signatures = signatures

    def detect(self, html: str) -> tuple[str, float, list[str]]:
        ranked = []
        for signature in self.signatures:
            matches = [i for i, p in enumerate(signature.patterns) if re.search(p, html, re.IGNORECASE)]
            if matches:
                ranked.append((len(matches), signature.id, matches))
        if not ranked:
            return "unknown", 0, []
        ranked.sort(reverse=True)
        count, engine, matches = ranked[0]
        return engine, min(0.95, count * 0.3), [f"signature:{engine}:{i}" for i in matches]
