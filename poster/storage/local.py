import logging
from pathlib import Path

from ..project import Project, load_project

log = logging.getLogger(__name__)


class Registry:
    """Replace immutable project snapshots; invalid updates retain the last valid revision."""

    def __init__(self, root: Path):
        self.root = root
        self.projects: dict[str, Project] = {}
        self.errors: dict[str, str] = {}

    def poll(self) -> list[Project]:
        self.root.mkdir(parents=True, exist_ok=True)
        found, changed, seen_ids = {}, [], set()
        self.errors = {}
        for folder in sorted(self.root.iterdir()):
            if folder.name.startswith(".") or not folder.is_dir():
                continue
            # A directory without READY is an incomplete upload, not a deletion.
            if not (folder / "READY").is_file():
                if folder.name in self.projects:
                    found[folder.name] = self.projects[folder.name]
                continue
            try:
                p = load_project(folder)
                if p.site.id != folder.name or p.site.id in seen_ids:
                    raise ValueError("Folder name must equal unique project id")
                seen_ids.add(p.site.id)
                verified = not p.site.recognition or (
                    p.site.recognition.state == "HTTP_VERIFIED" and p.site.recognition.http_verified
                )
                if p.site.enabled and p.site.status == "HTTP_READY" and p.site.mode == "http" and verified:
                    found[p.site.id] = p
                    if p.site.id not in self.projects or self.projects[p.site.id].revision != p.revision:
                        changed.append(p)
            except Exception:
                self.errors[folder.name] = "Invalid/incomplete project; last valid revision retained"
                if folder.name in self.projects:
                    found[folder.name] = self.projects[folder.name]
                log.warning("Project %s: invalid/incomplete revision", folder.name)
        self.projects = found
        return changed
