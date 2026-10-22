"""Only a closed, value-free rule schema can enter the global library."""

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Rule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^form-[a-f0-9]{16}$")
    version: int = Field(default=1, ge=1)
    scope: Literal["form-structure"] = "form-structure"
    algorithm: Literal["semantic-editor-pair-v1"] = "semantic-editor-pair-v1"
    editor: Literal["input", "textarea", "contenteditable", "iframe:contenteditable", "iframe:textarea"]
    roles: list[Literal["title", "body", "submit", "category", "tags", "csrf"]]
    source: Literal["browser-observation", "manual-observation"]
    tests: list[Literal["browser-permalink", "http-permalink"]] = []
    confidence: float = Field(default=0.5, ge=0, le=1)
    browser_successes: int = Field(default=0, ge=0)
    http_successes: int = Field(default=0, ge=0)
    failures: int = Field(default=0, ge=0)
    title_text_index: int = Field(default=0, ge=0, le=30)


class KnowledgeBase:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS recognition_rules(id TEXT PRIMARY KEY, rule TEXT NOT NULL)"
        )

    def rules(self):
        return [
            Rule.model_validate_json(r[0])
            for r in self.db.execute("SELECT rule FROM recognition_rules ORDER BY id")
        ]

    def propose(self, candidate, *, manual=False):
        editor = candidate.fields["body"].kind
        roles = sorted(candidate.fields)
        title_index = candidate.fields["title"].position
        rule_id = "form-" + hashlib.sha256(json.dumps([editor, roles, title_index]).encode()).hexdigest()[:16]
        existing = next((r for r in self.rules() if r.id == rule_id), None)
        rule = existing or Rule(
            id=rule_id,
            editor=editor,
            roles=roles,
            source="manual-observation" if manual else "browser-observation",
            title_text_index=title_index,
        )
        rule.browser_successes += 1
        if "browser-permalink" not in rule.tests:
            rule.tests.append("browser-permalink")
        rule.confidence = min(0.9, 0.5 + 0.05 * rule.browser_successes)
        self.save(rule)
        return rule.id

    def save(self, rule: Rule):
        rule = Rule.model_validate(rule.model_dump())
        # Fields are all enums/counts and a digest-based ID: no selectors, URL, field values or sessions.
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO recognition_rules VALUES(?,?)", (rule.id, rule.model_dump_json())
            )

    def match(self, candidate):
        if "body" not in candidate.fields:
            return None
        return next(
            (
                r
                for r in self.rules()
                if r.editor == candidate.fields["body"].kind and set(r.roles) <= set(candidate.fields)
            ),
            None,
        )

    def suggest(self, candidate):
        """Resolve an unmapped title by a demonstrated structural position, as a candidate only."""
        if "title" in candidate.fields or "body" not in candidate.fields:
            return None
        compatible = [
            r
            for r in self.rules()
            if r.editor == candidate.fields["body"].kind
            and (set(r.roles) - {"title"}) <= set(candidate.fields)
            and r.title_text_index < len(candidate.text_inputs)
        ]
        if len({r.title_text_index for r in compatible}) != 1:
            return None
        rule = compatible[0]
        candidate.fields["title"] = candidate.text_inputs[rule.title_text_index]
        candidate.evidence.append("learned-title-position")
        return rule

    def result(self, rule_id, succeeded):
        rule = next(r for r in self.rules() if r.id == rule_id)
        if succeeded:
            rule.http_successes += 1
            if "http-permalink" not in rule.tests:
                rule.tests.append("http-permalink")
        else:
            rule.failures += 1
        self.save(rule)

    def close(self):
        self.db.close()
