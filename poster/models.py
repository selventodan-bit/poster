"""Versioned, deliberately small data-only workflow language."""

from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class Schedule(StrictModel):
    max_concurrent: int = Field(default=1, ge=1, le=100)
    min_interval_seconds: float = Field(default=0, ge=0, le=86400)


class Site(StrictModel):
    schema_version: Literal[1] = 1
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,80}$")
    base_url: str
    allowed_hosts: list[str] = Field(min_length=1)
    mode: Literal["http", "browser"] = "http"
    status: Literal["NEW", "HTTP_READY", "REVIEW", "BROWSER_REQUIRED", "DISABLED"] = "REVIEW"
    enabled: bool = True
    workflow: str = "workflow.yaml"
    auth_file: str | None = None
    campaign: str | None = None
    schedule: Schedule = Schedule()
    timeout: float = Field(default=20, gt=0, le=300)
    get_retries: int = Field(default=2, ge=0, le=5)
    retry_delay: float = Field(default=0.25, ge=0, le=60)
    headers: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def urls(self):
        u = urlsplit(self.base_url)
        if u.scheme not in ("http", "https") or u.username or u.password or not u.hostname:
            raise ValueError("base_url must be an HTTP(S) URL without credentials")
        if any(not h or any(c in h for c in "/:*@?#") for h in self.allowed_hosts):
            raise ValueError("allowed_hosts must contain exact hostnames (no ports/wildcards)")
        if u.hostname.lower() not in [h.lower() for h in self.allowed_hosts]:
            raise ValueError("base_url is outside allowed_hosts")
        for name in self.headers:
            if name.lower() in {"cookie", "authorization", "proxy-authorization", "host"}:
                raise ValueError("Sensitive headers belong in encrypted session, not YAML")
        return self


class Step(StrictModel):
    id: str
    op: Literal["request", "extract", "set", "assert", "verify", "wait"]
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"] = "GET"
    url: str | None = None
    form: dict[str, Any] | None = None
    json_body: Any = Field(default=None, alias="json")
    multipart: dict[str, str] | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    save_as: str | None = None
    source: str | None = None
    kind: Literal["css_text", "css_attribute", "json_path", "regex", "contains", "status"] | None = None
    selector: str | None = None
    attribute: str | None = None
    path: str | None = None
    pattern: str | None = None
    expected: Any = None
    values: dict[str, Any] | None = None
    seconds: float = Field(default=0, ge=0, le=60)
    result_url: bool = False

    @model_validator(mode="after")
    def shape(self):
        if self.op == "request":
            if not self.url or not self.save_as:
                raise ValueError("request needs url and save_as")
            if sum(x is not None for x in (self.form, self.json_body, self.multipart)) > 1:
                raise ValueError("Choose one request encoding")
            for name in self.headers:
                if name.lower() in {"cookie", "authorization", "proxy-authorization", "host"}:
                    raise ValueError("Sensitive headers are not allowed in steps")
        if self.op in {"extract", "assert", "verify"}:
            if not self.source or not self.kind:
                raise ValueError("extract/assert/verify need source and kind")
            if self.kind.startswith("css_") and not self.selector:
                raise ValueError("CSS rule needs selector")
            if self.kind == "css_attribute" and not self.attribute:
                raise ValueError("css_attribute needs attribute")
            if self.kind == "json_path" and not self.path:
                raise ValueError("json_path needs path")
            if self.kind == "regex" and (not self.pattern or len(self.pattern) > 500):
                raise ValueError("regex needs an explicit pattern up to 500 characters")
            if self.op == "extract" and not self.save_as:
                raise ValueError("extract needs save_as")
            if self.op == "verify" and self.kind == "status":
                raise ValueError("HTTP status alone is not verification")
        if self.op == "set" and not self.values:
            raise ValueError("set needs values")
        return self


class Workflow(StrictModel):
    schema_version: Literal[1] = 1
    steps: list[Step] = Field(min_length=1, max_length=100)
    recovery: list[Step] = Field(default_factory=list, max_length=30)

    @model_validator(mode="after")
    def safe_recovery(self):
        if len({s.id for s in self.steps}) != len(self.steps):
            raise ValueError("Step ids must be unique")
        if not any(s.op == "verify" for s in self.steps):
            raise ValueError("Workflow requires a meaningful verify step")
        if self.recovery and not any(s.op == "verify" for s in self.recovery):
            raise ValueError("Recovery requires verify")
        if any(s.op == "request" and s.method not in {"GET", "HEAD"} for s in self.recovery):
            raise ValueError("Recovery is read-only")
        return self


class ContentOutput(StrictModel):
    title: str = "{{product.name}}"
    body: Literal["template_rendered"] = "template_rendered"


class Campaign(StrictModel):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,80}$")
    locale: str = Field(pattern=r"^[a-z]{2,3}(?:-[A-Za-z0-9]+)*$")
    products_file: str
    descriptions_file: str
    keywords_file: str
    cities_file: str
    intros_file: str
    templates: list[str] = Field(min_length=1, max_length=100)
    output: ContentOutput = ContentOutput()
    variables: dict[str, str] = Field(default_factory=dict)


def inside(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()) or Path(relative).is_absolute():
        raise ValueError("Path escapes project root")
    return path


def read_yaml(path: Path) -> dict:
    if path.stat().st_size > 2_000_000:
        raise ValueError("YAML exceeds 2 MB")
    data = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError("Expected YAML mapping")
    return data
