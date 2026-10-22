"""Data-only, seeded text generation. This module knows nothing about sites or HTTP."""

import csv
import hashlib
import json
import io
import random
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from ..models import Campaign, inside

MACRO = re.compile(r"{{\s*([a-zA-Z_][\w.]*)(?::(\d+))?\s*}}")
OPTIONAL = re.compile(r"\[\[optional:(0(?:\.\d+)?|1(?:\.0+)?)\]\](.*?)\[\[/optional\]\]", re.S)


def load_rows(path: Path, raw: bytes | None = None) -> list[dict]:
    text = (path.read_bytes() if raw is None else raw).decode("utf-8-sig")
    if path.suffix.lower() == ".csv":
        return list(csv.DictReader(io.StringIO(text)))
    if path.suffix.lower() in {".yaml", ".yml"}:
        data = yaml.safe_load(text)
        if not isinstance(data, list) or not all(isinstance(x, dict) for x in data):
            raise ValueError("Table YAML must be a list of mappings")
        return data
    raise ValueError("Table must be CSV or YAML")


def load_words(path: Path, raw: bytes | None = None) -> list[str]:
    text = (path.read_bytes() if raw is None else raw).decode("utf-8-sig")
    if path.suffix.lower() == ".csv":
        rows = list(csv.DictReader(io.StringIO(text)))
        if not rows or not ("text" in rows[0] or "value" in rows[0]):
            raise ValueError("Dictionary CSV needs text or value column")
        data = [r.get("text", r.get("value", "")) for r in rows]
    elif path.suffix.lower() in {".yaml", ".yml"}:
        data = yaml.safe_load(text)
        if not isinstance(data, list) or not all(isinstance(x, str) for x in data):
            raise ValueError("Dictionary YAML must be a list of strings")
    else:
        data = text.splitlines()
    words = list(dict.fromkeys(x.strip() for x in data if x.strip() and not x.startswith("#")))
    if not words:
        raise ValueError("Empty dictionary")
    return words


@dataclass(frozen=True)
class Content:
    title: str
    body: str
    metadata: dict

    @property
    def content_id(self):
        return hashlib.sha256(
            json.dumps(self.__dict__, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()


class Generator:
    def __init__(self, root: Path):
        self.root = root
        self._snapshot = None
        self._revision = None

    def snapshot(self):
        """READY gives multi-file atomicity; a bad new revision retains the last valid snapshot."""
        marker = self.root / "READY"
        if not marker.exists():
            return self._snapshot
        try:
            raw = marker.read_bytes()
            revision = hashlib.sha256(raw).hexdigest()
            if revision == self._revision:
                return self._snapshot
            ready = json.loads(raw)
            if ready.get("schema_version") != 1 or len(ready["files"]) > 2000:
                raise ValueError("Invalid content READY")
            candidate = {}
            for name, expected in ready["files"].items():
                path = inside(self.root, name)
                if path.stat().st_size > 5_000_000:
                    raise ValueError("Content file too large")
                data = path.read_bytes()
                if hashlib.sha256(data).hexdigest() != expected:
                    raise ValueError("Incomplete content upload")
                candidate[name] = data
            return candidate, revision
        except (OSError, KeyError, ValueError):
            if self._snapshot is None:
                raise ValueError("No valid content snapshot") from None
            return self._snapshot

    def generate(self, campaign_id: str, seed: int) -> Content:
        try:
            return self._generate(campaign_id, seed)
        except (ValueError, KeyError, TypeError, OSError, yaml.YAMLError):
            if self._snapshot is None:
                raise
            return self._generate(campaign_id, seed, use_previous=True)

    def _generate(self, campaign_id: str, seed: int, *, use_previous=False) -> Content:
        if not re.fullmatch(r"[\w-]+", campaign_id):
            raise ValueError("Invalid campaign id")
        new = self._snapshot if use_previous else self.snapshot()
        candidate_revision = None
        if isinstance(new, tuple):
            blobs, candidate_revision = new
        else:
            blobs = new
        used_files = {}

        def file(name):
            path = inside(self.root, name)
            raw = blobs[name] if blobs is not None else path.read_bytes()
            if len(raw) > 5_000_000:
                raise ValueError("Content file too large")
            used_files[name] = hashlib.sha256(raw).hexdigest()
            return raw

        config = Campaign.model_validate(yaml.safe_load(file(f"campaigns/{campaign_id}.yaml"))).model_dump()
        if config["id"] != campaign_id:
            raise ValueError("Campaign id does not match filename")
        rng = random.Random(seed)
        products = [
            p
            for p in load_rows(Path(config["products_file"]), file(config["products_file"]))
            if p["locale"] == config["locale"]
        ]
        if not products:
            raise ValueError("No products for campaign locale")
        product = rng.choice(products)
        descriptions = [
            d
            for d in load_rows(Path(config["descriptions_file"]), file(config["descriptions_file"]))
            if d["product_id"] == product["id"] and d.get("locale", config["locale"]) == config["locale"]
        ]
        if not descriptions:
            raise ValueError("Missing description for selected product/locale")
        description = rng.choice(descriptions)["description"]
        dictionaries = {
            name: load_words(Path(config[f"{name}_file"]), file(config[f"{name}_file"]))
            for name in ("keywords", "cities", "intros")
        }
        template_name = rng.choice(config["templates"])
        template = file(template_name).decode("utf-8-sig")
        if set(config.get("variables", {})) & {
            "product",
            "description",
            "intro",
            "keywords",
            "cities",
            "intros",
        }:
            raise ValueError("Campaign variables cannot override bound product/locale dictionaries")
        if config.get("output", {}).get("body", "template_rendered") != "template_rendered":
            raise ValueError("output.body must be template_rendered")
        context = {
            "product": product,
            "description": description,
            "intro": rng.choice(dictionaries["intros"]),
            **{k: v for k, v in config.get("variables", {}).items()},
        }
        selected = {}

        def expand(text, depth=0):
            if depth > 5:
                raise ValueError("Recursive content macro")
            text = OPTIONAL.sub(lambda m: expand(m[2], depth + 1) if rng.random() < float(m[1]) else "", text)

            def replace(m):
                name, count = m[1], m[2]
                if name in dictionaries:
                    n = int(count) if count else len(dictionaries[name])
                    if n > len(dictionaries[name]) or n < 1:
                        raise ValueError(f"Insufficient distinct values for {name}:{n}")
                    choices = rng.sample(dictionaries[name], n)
                    selected.setdefault(name, []).append(choices)
                    return "\n".join(expand(c, depth + 1) for c in choices)
                if count:
                    raise ValueError("Count macro applies only to dictionaries")
                value = context
                for key in name.split("."):
                    if not isinstance(value, dict) or key not in value:
                        raise ValueError(f"Unknown content macro: {name}")
                    value = value[key]
                return expand(str(value), depth + 1) if "{{" in str(value) else str(value)

            rendered = MACRO.sub(replace, text)
            if "{{" in rendered or "[[optional:" in rendered:
                raise ValueError("Unsupported macro syntax")
            return rendered.strip()

        title = expand(config.get("output", {}).get("title", "{{product.name}}"))
        body = expand(template)
        metadata = {
            "campaign": campaign_id,
            "locale": config["locale"],
            "seed": seed,
            "template": template_name,
            "product_id": product["id"],
            "product": product,
            "description": description,
            "intro": context["intro"],
            "selected": selected,
            "source_revision": hashlib.sha256(json.dumps(used_files, sort_keys=True).encode()).hexdigest(),
        }
        if candidate_revision is not None:
            self._snapshot, self._revision = blobs, candidate_revision
        return Content(title, body, metadata)
