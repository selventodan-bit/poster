import base64
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import yaml

from .models import Site, Workflow, inside


@dataclass(frozen=True)
class Project:
    site: Site
    workflow: Workflow
    revision: str
    session: bytes | None = None
    targets: str = ""

    def serialize(self) -> str:
        return json.dumps(
            {
                "site": self.site.model_dump(),
                "workflow": self.workflow.model_dump(by_alias=True),
                "revision": self.revision,
                "session": base64.b64encode(self.session).decode() if self.session else None,
                "targets": self.targets,
            }
        )

    @classmethod
    def deserialize(cls, raw: str):
        d = json.loads(raw)
        return cls(
            Site.model_validate(d["site"]),
            Workflow.model_validate(d["workflow"]),
            d["revision"],
            base64.b64decode(d["session"]) if d["session"] else None,
            d["targets"],
        )


def manifest(root: Path) -> dict[str, str]:
    result = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError("Symlinks are not allowed in projects")
        if path.is_file() and path.name != "READY":
            if path.stat().st_size > 5_000_000:
                raise ValueError("Project file exceeds 5 MB")
            result[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def mark_ready(root: Path):
    """Publish locally only after all files are in place; READY hashes prevent mixed revisions."""
    tmp = root / ".READY.tmp"
    tmp.unlink(missing_ok=True)
    payload = json.dumps({"schema_version": 1, "files": manifest(root)}, sort_keys=True)
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(root / "READY")


def load_project(root: Path) -> Project:
    ready_raw = (root / "READY").read_bytes()
    if len(ready_raw) > 1_000_000:
        raise ValueError("Manifest too large")
    ready = json.loads(ready_raw)
    files = ready["files"]
    if ready.get("schema_version") != 1 or not isinstance(files, dict) or len(files) > 200:
        raise ValueError("Invalid READY manifest")
    # Read once into an immutable snapshot, checking every byte against READY.
    blobs = {}
    for name, checksum in files.items():
        path = inside(root, name)
        if path.is_symlink() or path.stat().st_size > 5_000_000:
            raise ValueError("Invalid project file")
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != checksum:
            raise ValueError("Incomplete upload: READY checksum mismatch")
        blobs[name] = raw
    site = Site.model_validate(yaml.safe_load(blobs["site.yaml"]))
    inside(root, site.workflow)
    workflow = Workflow.model_validate(yaml.safe_load(blobs[site.workflow]))
    if site.auth_file:
        inside(root, site.auth_file)
    return Project(
        site,
        workflow,
        hashlib.sha256(ready_raw).hexdigest(),
        blobs[site.auth_file] if site.auth_file else None,
        blobs.get("targets.csv", b"").decode("utf-8-sig"),
    )
