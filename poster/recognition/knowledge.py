"""Migratable, value-free knowledge. Imported rules are hypotheses, never HTTP credentials or proof."""

import hashlib
import hmac
import json
import re
import secrets
import sqlite3
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field
from .transfer import HTTPTemplate, compile_template

Family = Literal[
    "unknown",
    "xenforo",
    "phpbb",
    "vbulletin",
    "mybb",
    "discourse",
    "smf",
    "invision",
    "flarum",
    "wordpress",
    "wpforo",
]
Editor = Literal["native", "tinymce", "ckeditor", "quill", "prosemirror", "summernote", "froala"]
EvidenceID = Annotated[str, Field(pattern=r"^[a-f0-9]{16}$")]
Feature = Literal[
    "previous-stage",
    "single-line",
    "multiline-editor",
    "semantic-attributes",
    "accessible-label",
    "before-editor",
    "after-editor",
    "unique-title-near-editor",
    "form-submit-control",
    "accessible-publication-intent",
]
NavFeature = Literal[
    "accessible-compose-intent",
    "compose-route",
    "compose-affordance",
    "controlled-editor",
    "controlled-menu",
    "navigation-link",
    "non-submit-control",
    "navigation-context",
]


class Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class EngineKnowledge(Closed):
    family: Family = "unknown"
    version: str | None = Field(default=None, pattern=r"^\d{1,3}(?:\.\d{1,3}){0,3}$")
    adapter_revision: int = Field(default=2, ge=1, le=100)
    channels: list[Literal["generator", "assets", "routes", "api", "dom", "behavior", "headers"]] = Field(
        default_factory=list, max_length=7
    )


class NavigationStep(Closed):
    purpose: Literal["menu", "compose"]
    evidence: list[NavFeature] = Field(max_length=8)
    score: float = Field(ge=0, le=1)


class NavigationKnowledge(Closed):
    steps: list[NavigationStep] = Field(default_factory=list, max_length=5)


class FormKnowledge(Closed):
    editor_adapter: Editor = "native"
    title_features: list[Feature] = Field(default_factory=list, max_length=10)
    layout: list[Literal["light", "shadow", "dialog", "inline"]] = Field(default_factory=list, max_length=4)
    input_count: int = Field(default=0, ge=0, le=100)


class SubmissionOperation(Closed):
    method: Literal["POST", "PUT", "PATCH", "DELETE"]
    encoding: Literal["json", "form", "multipart"]
    roles: list[Literal["title", "body"]] = Field(max_length=2)
    body_shape: list[Literal["object", "array", "scalar", "dynamic", "title", "body"]] = Field(
        default_factory=list, max_length=100
    )
    route: Literal["draft", "publish", "other"] = "other"
    route_shape: list[
        Literal["api", "draft", "drafts", "publish", "threads", "topics", "posts", "identity", "other"]
    ] = Field(default_factory=list, max_length=30)


class SubmissionDependency(Closed):
    parent: int = Field(ge=0, le=20)
    child: int = Field(ge=0, le=20)
    source: Literal["json_path", "header"]
    target: Literal["payload", "path", "query", "header"]


class SubmissionKnowledge(Closed):
    operations: list[SubmissionOperation] = Field(default_factory=list, max_length=20)
    dependencies: list[SubmissionDependency] = Field(default_factory=list, max_length=100)


class TokenPattern(Closed):
    name: Literal[
        "csrf_token",
        "csrf",
        "nonce",
        "token",
        "x-csrf-token",
        "x-xsrf-token",
        "id",
        "draft_id",
        "topic_id",
        "uuid",
        "other",
    ]
    role: Literal["csrf", "identity", "nonce"]
    source: Literal["html-hidden", "html-meta", "json", "header", "cookie", "unknown"]
    fresh: bool = True


class TokenKnowledge(Closed):
    patterns: list[TokenPattern] = Field(default_factory=list, max_length=100)


class VerificationKnowledge(Closed):
    permalink: Literal["json", "bookmark", "redirect", "unknown"] = "unknown"
    content: list[Literal["title", "body"]] = Field(default_factory=lambda: ["title", "body"], max_length=2)
    independent_get: bool = True
    rejected: list[
        Literal["http-error", "login", "explicit-error", "missing-content", "missing-permalink"]
    ] = Field(default_factory=list, max_length=5)


class Rule(Closed):
    id: str = Field(pattern=r"^form-[a-f0-9]{16}$")
    version: int = Field(default=2, ge=1, le=2)
    scope: Literal["form-structure"] = "form-structure"
    algorithm: Literal["semantic-editor-pair-v1", "relational-workflow-v2"] = "relational-workflow-v2"
    editor: Literal["input", "textarea", "contenteditable", "iframe:contenteditable", "iframe:textarea"]
    roles: list[Literal["title", "body", "submit", "category", "tags", "csrf"]] = Field(max_length=6)
    source: Literal["browser-observation", "manual-observation"]
    tests: list[Literal["browser-permalink", "http-permalink"]] = Field(default_factory=list, max_length=2)
    confidence: float = Field(default=0.5, ge=0, le=1)
    browser_successes: int = Field(default=0, ge=0)
    http_successes: int = Field(default=0, ge=0)
    failures: int = Field(default=0, ge=0)
    title_text_index: int = Field(default=0, ge=0, le=30)
    enabled: bool = True
    priority: int = Field(default=0, ge=-100, le=100)
    revision: int = Field(default=1, ge=1)
    regressions: int = Field(default=0, ge=0)
    installations: list[EvidenceID] = Field(default_factory=list, max_length=100)
    http_installations: list[EvidenceID] = Field(default_factory=list, max_length=100)
    variants: list[EvidenceID] = Field(default_factory=list, max_length=100)
    engine: EngineKnowledge = Field(default_factory=EngineKnowledge)
    navigation: NavigationKnowledge = Field(default_factory=NavigationKnowledge)
    form: FormKnowledge = Field(default_factory=FormKnowledge)
    submission: SubmissionKnowledge = Field(default_factory=SubmissionKnowledge)
    tokens: TokenKnowledge = Field(default_factory=TokenKnowledge)
    verification: VerificationKnowledge = Field(default_factory=VerificationKnowledge)
    http_templates: list[HTTPTemplate] = Field(default_factory=list, max_length=8)

    @property
    def maturity(self):
        if self.regressions and len(self.http_installations) >= 2 and self.engine.family != "unknown":
            return "regression-tested"
        if len(self.http_installations) >= 2 and self.engine.family != "unknown":
            return "independent-installations"
        if len(self.variants) >= 2 and self.browser_successes >= 2:
            return "independent-variants"
        return "single-site" if self.browser_successes else "observed"


class Match(Closed):
    rule: Rule
    relation: Literal["exact", "partial", "compatible", "conflict", "unknown"]
    score: float
    evidence: list[
        Literal[
            "editor", "roles", "layout", "title-relation", "ordinal-fallback", "negative-evidence", "engine"
        ]
    ] = Field(default_factory=list)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()[:16]


class KnowledgeBase:
    schema_version = 2

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version > self.schema_version:
            self.db.close()
            raise ValueError("Knowledge database is newer than this application")
        with self.db:
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS recognition_rules(id TEXT PRIMARY KEY,rule TEXT NOT NULL)"
            )
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS knowledge_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)"
            )
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS rule_history(id INTEGER PRIMARY KEY,rule_id TEXT,rule TEXT NOT NULL)"
            )
            self.db.execute("INSERT OR IGNORE INTO knowledge_meta VALUES('salt',?)", (secrets.token_hex(32),))
            self.db.execute("PRAGMA user_version=2")
            # Parse old rows with additive defaults, preserving counters and identifiers.
            for rule_id, raw in self.db.execute("SELECT id,rule FROM recognition_rules").fetchall():
                rule = Rule.model_validate_json(raw)
                rule.version = 2
                self.db.execute(
                    "UPDATE recognition_rules SET rule=? WHERE id=?", (rule.model_dump_json(), rule_id)
                )

    def rules(self):
        return [
            Rule.model_validate_json(r[0])
            for r in self.db.execute("SELECT rule FROM recognition_rules ORDER BY id")
        ]

    def installation(self, origin):
        salt = self.db.execute("SELECT value FROM knowledge_meta WHERE key='salt'").fetchone()[0]
        return hmac.new(bytes.fromhex(salt), origin.encode(), hashlib.sha256).hexdigest()[:16]

    def save(self, rule):
        rule = Rule.model_validate(rule.model_dump())
        if any(not re.fullmatch(r"[a-f0-9]{16}", v) for v in rule.installations + rule.variants):
            raise ValueError("Only opaque evidence identifiers are permitted")
        with self.db:
            previous = self.db.execute("SELECT rule FROM recognition_rules WHERE id=?", (rule.id,)).fetchone()
            if previous and previous[0] != rule.model_dump_json():
                self.db.execute("INSERT INTO rule_history(rule_id,rule) VALUES(?,?)", (rule.id, previous[0]))
                rule.revision = Rule.model_validate_json(previous[0]).revision + 1
            self.db.execute(
                "INSERT OR REPLACE INTO recognition_rules VALUES(?,?)", (rule.id, rule.model_dump_json())
            )

    def propose(self, candidate, *, manual=False, origin="", engine="unknown", navigation=(), capture=None):
        editor = candidate.fields["body"].kind
        roles = sorted(candidate.fields)
        index = candidate.fields["title"].position
        title_features = candidate.fields["title"].features
        identity = [
            engine,
            editor,
            roles,
            index,
            candidate.fields["body"].editor,
            sorted(set(title_features) & {"before-editor", "after-editor"}),
            sorted(candidate.structure),
        ]
        rule_id = "form-" + digest(identity)
        rule = next((r for r in self.rules() if r.id == rule_id), None) or Rule(
            id=rule_id,
            editor=editor,
            roles=roles,
            source="manual-observation" if manual else "browser-observation",
            title_text_index=index,
        )
        rule.browser_successes += 1
        rule.tests = list(dict.fromkeys([*rule.tests, "browser-permalink"]))
        rule.confidence = max(0.05, min(0.95, 0.5 + 0.05 * rule.browser_successes - 0.1 * rule.failures))
        rule.form = FormKnowledge(
            editor_adapter=candidate.fields["body"].editor,
            title_features=title_features,
            layout=candidate.structure,
            input_count=len(candidate.text_inputs),
        )
        rule.engine = EngineKnowledge(family=engine)
        rule.navigation = NavigationKnowledge(steps=list(navigation))
        if origin:
            rule.installations = list(dict.fromkeys([*rule.installations, self.installation(origin)]))[-100:]
        rule.variants = list(
            dict.fromkeys(
                [
                    *rule.variants,
                    digest([candidate.structure, title_features, len(candidate.text_inputs), editor]),
                ]
            )
        )[-100:]
        if capture:
            self.observe_protocol(rule, capture)
        self.save(rule)
        return rule.id

    @staticmethod
    def observe_protocol(rule, capture):
        from .network import NetworkAnalyzer

        analysis = NetworkAnalyzer().analyze(capture)
        operations = []

        def structure(value):
            if isinstance(value, dict):
                return ["object", *[part for v in value.values() for part in structure(v)]]
            if isinstance(value, list):
                return ["array", *[part for v in value for part in structure(v)]]
            return [
                "title"
                if isinstance(value, str) and capture.title_marker in value
                else "body"
                if isinstance(value, str) and capture.body_marker in value
                else "scalar"
            ]

        for w in analysis.writes:
            shape = structure(w["payload"])
            from urllib.parse import urlsplit

            segments = urlsplit(w["url"]).path.split("/")
            route_shape = [
                s
                if s in {"api", "draft", "drafts", "publish", "threads", "topics", "posts"}
                else "identity"
                if re.fullmatch(r"[a-f0-9-]{16,}|\d+", s)
                else "other"
                for s in segments
                if s
            ]
            operations.append(
                SubmissionOperation(
                    method=w["method"],
                    encoding=w["encoding"],
                    roles=sorted(w["roles"]),
                    body_shape=shape[:100],
                    route="draft" if "draft" in w["url"] else "publish" if w["roles"] else "other",
                    route_shape=route_shape[:30],
                )
            )
        ids = {w["id"]: i for i, w in enumerate(analysis.writes)}
        rule.submission = SubmissionKnowledge(
            operations=operations,
            dependencies=[
                SubmissionDependency(
                    parent=ids[e.parent], child=ids[e.child], source=e.source_kind, target=e.target_kind
                )
                for e in analysis.dependencies
                if e.parent in ids and e.child in ids
            ],
        )
        confirmation = capture.confirmation
        rule.verification = VerificationKnowledge(
            permalink="json"
            if confirmation.get("url_path")
            else "bookmark"
            if confirmation.get("link_selector")
            else "redirect"
        )

    def observe_workflow(self, rule_id, workflow):
        rule = next(r for r in self.rules() if r.id == rule_id)
        patterns = []
        for step in workflow.get("steps", []):
            if step["op"] != "extract" or not step.get("save_as", "").startswith("dynamic_"):
                continue
            kind = step["kind"]
            source = (
                "html-hidden"
                if step.get("attribute") == "value"
                else "html-meta"
                if kind == "css_attribute"
                else "header"
                if kind == "header"
                else "cookie"
                if kind == "cookie"
                else "json"
            )

            name = (
                step.get("path", "").rsplit(".", 1)[-1]
                if kind == "json_path"
                else step.get("attribute", "")
                if kind == "header"
                else next(iter(re.findall(r'name=["\']([^"\']+)', step.get("selector", ""))), "other")
            )
            allowed = TokenPattern.model_fields["name"].annotation.__args__
            patterns.append(
                TokenPattern(
                    name=name if name in allowed else "other",
                    role="csrf" if source.startswith("html") or source == "header" else "identity",
                    source=source,
                )
            )
        rule.tokens = TokenKnowledge(patterns=patterns)
        self.save(rule)

    def observe_engine(self, rule_id, technology):
        rule = next(r for r in self.rules() if r.id == rule_id)
        rule.engine = EngineKnowledge(
            family=technology["family"],
            version=technology.get("version"),
            adapter_revision=technology["adapter_revision"],
            channels=technology["evidence"],
        )
        self.save(rule)

    def record_template(self, rule_id, capture, workflow):
        rule = next(r for r in self.rules() if r.id == rule_id)
        template = compile_template(capture, workflow, rule.http_templates)
        if template and template not in rule.http_templates:
            rule.http_templates = [*rule.http_templates, template][-8:]
            self.save(rule)

    def http_candidates(self, candidate, *, engine="unknown"):
        return [m.rule for m in self.candidates(candidate, engine=engine) if m.rule.http_templates]

    def candidates(self, candidate, *, engine="unknown"):
        if "body" not in candidate.fields:
            return []
        matches = []
        for r in self.rules():
            if (
                not r.enabled
                or r.failures >= max(2, r.http_successes + 1)
                or r.editor != candidate.fields["body"].kind
            ):
                continue
            if r.engine.family not in {"unknown", engine} and engine != "unknown":
                continue
            evidence = ["editor"]
            score = 0.35
            if (set(r.roles) - {"title"}) <= candidate.fields.keys():
                score += 0.2
                evidence.append("roles")
            else:
                continue
            if r.form.editor_adapter != candidate.fields["body"].editor:
                continue
            if set(r.form.layout) == set(candidate.structure):
                score += 0.1
                evidence.append("layout")
            if r.form.input_count == len(candidate.text_inputs):
                score += 0.1
            score += r.priority / 1000 + min(0.1, r.http_successes * 0.02) - min(0.4, r.failures * 0.15)
            relation = (
                "exact"
                if r.form.input_count == len(candidate.text_inputs)
                and set(r.form.layout) == set(candidate.structure)
                else "compatible"
            )
            matches.append(Match(rule=r, relation=relation, score=score, evidence=evidence))
        return sorted(matches, key=lambda m: m.score, reverse=True)

    def match(self, candidate):
        ranked = self.candidates(candidate)
        return ranked[0].rule if ranked else None

    def suggest(self, candidate):
        if "title" in candidate.fields or "body" not in candidate.fields:
            return None
        ranked = self.candidates(candidate)
        proposals = []
        for match in ranked:
            r = match.rule
            # Prefer demonstrated relational features over a raw ordinal after reordering.
            relations = set(r.form.title_features) & {"before-editor", "after-editor"}
            refs = [f for f in candidate.text_inputs if relations and relations <= set(f.features)]
            if len(refs) == 1:
                ref = refs[0]
                match.evidence.append("title-relation")
            elif r.title_text_index < len(candidate.text_inputs) and (
                r.browser_successes or r.form.input_count
            ):
                ref = candidate.text_inputs[r.title_text_index]
                match.evidence.append("ordinal-fallback")
            else:
                continue
            proposals.append((match, ref))
        if not proposals:
            return None
        best, ref = proposals[0]
        if any(
            other_ref.selector != ref.selector and best.score - other.score < 0.15
            for other, other_ref in proposals[1:]
        ):
            candidate.evidence.append("knowledge-conflict")
            return None
        candidate.fields["title"] = ref
        ref.confidence = max(ref.confidence, 0.55)
        candidate.evidence.extend(["learned-title-position", "knowledge-" + best.relation])
        return best.rule

    def result(self, rule_id, succeeded, *, origin=""):
        rule = next(r for r in self.rules() if r.id == rule_id)
        if succeeded:
            rule.http_successes += 1
            if origin:
                rule.http_installations = list(
                    dict.fromkeys([*rule.http_installations, self.installation(origin)])
                )[-100:]
            rule.tests = list(dict.fromkeys([*rule.tests, "http-permalink"]))
        else:
            rule.failures += 1
        rule.confidence = max(0.05, min(0.95, 0.5 + 0.05 * rule.browser_successes - 0.15 * rule.failures))
        self.save(rule)

    def disable(self, rule_id, disabled=True):
        rule = next(r for r in self.rules() if r.id == rule_id)
        rule.enabled = not disabled
        self.save(rule)

    def regression(self, rule_id, passed):
        rule = next(r for r in self.rules() if r.id == rule_id)
        if passed:
            rule.regressions += 1
        else:
            rule.failures += 1
        self.save(rule)

    def export_rules(self, path):
        path.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "rules": [
                        r.model_dump(exclude={"installations", "http_installations"}) for r in self.rules()
                    ],
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    def import_rules(self, path):
        if path.stat().st_size > 2_000_000:
            raise ValueError("Knowledge import too large")
        data = json.loads(path.read_text(encoding="utf-8"))
        if (
            set(data) != {"schema_version", "rules"}
            or data["schema_version"] != 2
            or len(data["rules"]) > 1000
        ):
            raise ValueError("Invalid knowledge envelope")
        rules = [Rule.model_validate(r) for r in data["rules"]]
        existing = {r.id for r in self.rules()}
        for rule in rules:
            if rule.id in existing:
                continue
            # Foreign success counters cannot authorize local field mappings or publication.
            rule.browser_successes = rule.http_successes = rule.failures = rule.regressions = 0
            rule.installations = []
            rule.http_installations = []
            rule.variants = []
            rule.tests = []
            rule.priority = -10
            rule.confidence = 0.3
            self.save(rule)
        return len({r.id for r in rules} - existing)

    def backup(self, path):
        if path.exists():
            raise FileExistsError(path)
        with sqlite3.connect(path) as target:
            self.db.backup(target)
        with sqlite3.connect(path) as target:
            if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("Invalid knowledge backup")

    def close(self):
        self.db.close()
