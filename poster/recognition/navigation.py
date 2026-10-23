"""Bounded, explainable form navigation. Unclassified controls are never clicked."""

import asyncio
import re
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urljoin, urlsplit

from .localization import Localizations

NAV_JS = r"""() => {
 const all=root=>[...root.querySelectorAll('*')].flatMap(e=>[e,...(e.shadowRoot?all(e.shadowRoot):[])]);
 for(const e of all(document)) e.removeAttribute('data-poster-nav');
 return all(document).filter(e=>e.matches('a[href],button,[role="button"],[role="menuitem"],[role="tab"]'))
 .filter(e=>e.getClientRects().length && getComputedStyle(e).visibility!=='hidden').slice(0,300).map((e,i)=>{
   e.setAttribute('data-poster-nav','nav-'+i);
   const target=document.getElementById(e.getAttribute('aria-controls')||'') ||
     (e.getAttribute('data-target')?.startsWith('#')?document.getElementById(e.getAttribute('data-target').slice(1)):null);
   return {selector:'[data-poster-nav="nav-'+i+'"]',tag:e.tagName.toLowerCase(),type:e.type||'',
    role:e.getAttribute('role')||'',text:(e.innerText||'').slice(0,200),
    accessible:[e.getAttribute('aria-label'),e.title,...[...e.querySelectorAll('svg title')].map(n=>n.textContent)].filter(Boolean).join(' '),
    attrs:[e.id,e.className?.baseVal||e.className,e.getAttribute('data-action'),e.querySelector('svg use')?.getAttribute('href')].filter(Boolean).join(' ').slice(0,500),
    href:e.getAttribute('href')||'',method:e.getAttribute('data-method')||'',in_form:!!e.closest('form'),
    menu:!!e.closest('nav,[role="navigation"],[role="menu"],header,aside'),
    popup:e.getAttribute('aria-haspopup')||'',controls:!!target,
    target_editor:!!target?.querySelector('textarea,[contenteditable="true"],iframe'),
    target_menu:!!target?.matches('[role="menu"],nav'),
    relation:e.getAttribute('aria-expanded')||''};
 });
}"""


@dataclass
class NavigationCandidate:
    selector: str
    score: float
    purpose: str
    evidence: list[str] = field(default_factory=list)
    safe: bool = False


class NavigationDiscovery:
    def __init__(self, *, locales=None, max_actions=5, knowledge=None):
        self.locales = locales or Localizations()
        self.max_actions = max_actions
        self.trace = []
        self.knowledge = knowledge
        self.staged_fields = {}
        self.stop_reason = None

    def classify(self, node, origin):
        evidence, score = [], 0
        words = " ".join(str(node.get(k, "")) for k in ("text", "accessible", "attrs", "href"))
        parsed = urlsplit(urljoin(origin, node.get("href") or origin))
        if (parsed.scheme, parsed.netloc) != (urlsplit(origin).scheme, urlsplit(origin).netloc):
            return NavigationCandidate(node["selector"], 0, "foreign", safe=False)
        if self.locales.has("danger", words) or node.get("method", "").upper() not in {"", "GET"}:
            return NavigationCandidate(node["selector"], 0, "mutation", safe=False)
        query = parse_qs(parsed.query)
        if any(
            v.lower() in {"delete", "logout", "save", "submit", "publish", "vote"}
            for vals in query.values()
            for v in vals
        ):
            return NavigationCandidate(node["selector"], 0, "mutation", safe=False)
        stage_control = node.get("type") == "button" and (
            node.get("target_editor") or node.get("target_menu")
        )
        if (node.get("in_form") and not stage_control) or node.get("type") == "submit":
            return NavigationCandidate(node["selector"], 0, "submit", safe=False)
        if self.locales.has("compose", node.get("text", "") + " " + node.get("accessible", "")):
            evidence.append("accessible-compose-intent")
            score += 0.45
        if re.search(
            r"(?:new[-_/]?(?:topic|thread|discussion)|(?:topic|thread)[-_/]?(?:new|create)|posting\.php.*mode=post|newthread\.php|post-thread|action=post)",
            node.get("href", ""),
            re.I,
        ):
            evidence.append("compose-route")
            score += 0.55
        attrs = re.sub(r"([a-z])([A-Z])", r"\1-\2", node.get("attrs", ""))
        technical_compose = bool(
            re.search(r"(?:new|create)[-_](?:topic|thread|discussion)|compose", attrs, re.I)
        )
        if technical_compose or re.search(r"pencil|edit_square", attrs, re.I):
            evidence.append("compose-affordance")
            score += 0.55 if technical_compose else 0.3
        if node.get("target_editor"):
            evidence.append("controlled-editor")
            score += 0.65
        menu = node.get("popup") == "menu" or node.get("target_menu")
        if menu and node.get("controls"):
            evidence.append("controlled-menu")
            score += 0.65
        if (
            node.get("tag") == "a"
            and node.get("href")
            and not node["href"].lower().startswith(("javascript:", "data:"))
        ):
            evidence.append("navigation-link")
            score += 0.15
        elif node.get("type") == "button" or node.get("role") in {"tab", "menuitem"}:
            evidence.append("non-submit-control")
            score += 0.15
        if node.get("menu"):
            evidence.append("navigation-context")
            score += 0.1
        # An unknown icon or generic button is never made safe just by being clickable.
        intent = any(
            e in evidence
            for e in ("accessible-compose-intent", "compose-route", "controlled-editor", "controlled-menu")
        )
        intent = intent or (technical_compose and node.get("type") == "button")
        safe = intent and len(evidence) >= 2 and score >= 0.6
        return NavigationCandidate(
            node["selector"], min(0.99, score), "menu" if menu else "compose", evidence, safe
        )

    async def discover(self, page, inspect):
        forms = await inspect()
        visited = set()
        for _ in range(self.max_actions):
            if any(f.complete for f in forms):
                return forms
            nodes = await page.evaluate(NAV_JS)
            ranked = []
            destinations = set()
            for node in nodes:
                signature = tuple(
                    str(node.get(k, "")) for k in ("href", "attrs", "text", "accessible", "popup", "relation")
                )
                candidate = self.classify(node, page.url)
                if candidate.safe and signature not in visited:
                    if self.knowledge:
                        relevant = [
                            step
                            for rule in self.knowledge.rules()
                            if rule.enabled and rule.browser_successes and rule.failures < 2
                            for step in rule.navigation.steps
                            if step.purpose == candidate.purpose
                            and set(step.evidence) <= set(candidate.evidence)
                        ]
                        if relevant:
                            candidate.score = min(0.99, candidate.score + 0.05)
                    destination = urljoin(page.url, node["href"]) if node.get("href") else None
                    if destination and destination in destinations:
                        continue
                    if destination:
                        destinations.add(destination)
                    ranked.append((candidate, signature))
            ranked.sort(key=lambda c: c[0].score, reverse=True)
            if not ranked or (len(ranked) > 1 and ranked[0][0].score - ranked[1][0].score < 0.1):
                self.stop_reason = "NAVIGATION_AMBIGUOUS" if len(ranked) > 1 else "NO_FORM"
                break
            candidate, signature = ranked[0]
            for f in forms:
                if "title" in f.fields and f.fields["title"].confidence >= 0.7:
                    self.staged_fields[(f.key, "title")] = f.fields["title"]
            visited.add(signature)
            self.trace.append(
                {"purpose": candidate.purpose, "evidence": candidate.evidence, "score": candidate.score}
            )
            await page.locator(candidate.selector).click(timeout=2500)
            await page.wait_for_load_state("domcontentloaded", timeout=30000)
            for _ in range(6):
                await asyncio.sleep(0.1)
                forms = await inspect()
                for form in forms:
                    ref = self.staged_fields.get((form.key, "title"))
                    if (
                        ref
                        and "title" not in form.fields
                        and "body" in form.fields
                        and await page.frames[ref.frame].locator(ref.selector).count() == 1
                    ):
                        ref.features = list(dict.fromkeys([*ref.features, "previous-stage"]))
                        form.fields["title"] = ref
                        form.evidence.append("staged-title")
                        from .forms import UniversalFormDetector

                        form.confidence = UniversalFormDetector.confidence(form.fields)
                if forms:
                    break
        return forms
