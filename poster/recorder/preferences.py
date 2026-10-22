"""Small value-only desktop settings, stored outside exported projects."""

import json
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


class Preferences:
    def __init__(self, path: Path):
        self.path = path

    def load(self):
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            return {k: v for k, v in value.items() if k in {"url", "project_id"} and isinstance(v, str)}
        except (OSError, ValueError, AttributeError):
            return {}

    def save(self, url, project_id):
        parsed = urlsplit(url)
        clean_url = urlunsplit((parsed.scheme, parsed.netloc.rsplit("@", 1)[-1], parsed.path, "", ""))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"url": clean_url, "project_id": project_id}), encoding="utf-8")
        temporary.replace(self.path)
