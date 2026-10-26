"""GUI project registrations over the existing immutable snapshot service."""

import base64
import json
from pathlib import Path
import posixpath
import uuid

from cryptography.fernet import Fernet

from ..auth import session_key
from ..maker.model import Graph, secret_refs
from ..maker.storage import atomic
from ..maker.variables import has_reference
from .models import DispatcherError
from .security import plain_path, restrict, seal, unseal
from .service import enqueue_snapshot
from .snapshots import Snapshots


RESOURCE_NOTICE = (
    "Рабочая папка каждого задания изначально пуста. CSV/XLSX и другие входные файлы "
    "из Maker не копируются. Создайте их действиями графа или передайте содержимое через входы. "
    "Абсолютные пути Maker и выход за рабочую папку запрещены. "
    "Явно выбранные ресурсы подключаются через переносимый пакет Google Drive."
)


class Catalog:
    def __init__(self, store):
        self.store = store
        self.path = store.path.parent / "catalog.enc"
        try:
            self.items = unseal(self.path.read_bytes()) if self.path.exists() else {}
            if not isinstance(self.items, dict):
                raise ValueError()
        except Exception as exc:
            raise DispatcherError("CATALOG_INVALID") from exc

    def save(self):
        atomic(self.path, seal(self.items))
        restrict(self.path)

    def inspect(self, source, key_file=None):
        source = plain_path(source)
        snapshots = Snapshots(self.store.root)
        project_id, version = snapshots.create(source)
        manifest, files = snapshots.read(version)
        graph = Graph.model_validate_json(files[manifest["main"]])
        tokens = []
        pending, visited = [manifest["main"]], set()
        while pending:
            name = pending.pop()
            if name in visited:
                continue
            if name not in files:
                raise DispatcherError("PROJECT_INVALID")
            visited.add(name)
            dependency = Graph.model_validate_json(files[name])
            if dependency.secret_file:
                sidecar = posixpath.join(posixpath.dirname(name), dependency.secret_file)
                tokens.extend(
                    base64.b64decode(v, validate=True) for v in json.loads(files[sidecar])["tokens"].values()
                )
            for node in dependency.nodes:
                if node.type == "Project.Run":
                    value = node.params.get("path", "child.postergraph.json")
                    if has_reference(value):
                        pending.extend(n for n in files if n.endswith(".postergraph.json"))
                    elif isinstance(value, str):
                        pending.append(
                            posixpath.normpath(
                                posixpath.join(posixpath.dirname(name), value.replace("\\", "/"))
                            )
                        )
        project_key = None
        if tokens:
            candidates = [session_key()]
            paths = [plain_path(key_file)] if key_file else []
            if not key_file:
                for parent in [source.parent, *source.parents]:
                    paths += [parent / ".poster/private/session.key", parent / "private/session.key"]
            for path in paths:
                if path.is_file():
                    candidates.append(plain_path(path).read_bytes().strip())
            for key in candidates:
                try:
                    cipher = Fernet(key)
                    for token in tokens:
                        json.loads(cipher.decrypt(token))
                    project_key = key.decode("ascii")
                    break
                except Exception:
                    continue
            if project_key is None:
                raise DispatcherError("PROJECT_KEY_REQUIRED")
        # Public view intentionally has no default/input values or secret tokens.
        inputs = [
            {
                "name": n,
                "secret": v.secret or bool(secret_refs(v.value)),
                "required": v.required,
                "has_default": v.value is not None,
                "type": type(v.value).__name__ if not v.secret else "str",
            }
            for n, v in graph.variables.items()
            if v.input
        ]
        item_id = next((k for k, v in self.items.items() if v["source"] == str(source)), uuid.uuid4().hex)
        self.items[item_id] = {
            "id": item_id,
            "project_id": project_id,
            "name": graph.name,
            "version": version,
            "source": str(source),
            "inputs": inputs,
            "nodes": len(graph.nodes),
            "browser": any(
                n.type.startswith("Browser.")
                or n.type == "Logic.If"
                and n.params.get("condition") != "equals"
                for n in graph.nodes
            ),
            "project_key": project_key,
            "resources": RESOURCE_NOTICE,
        }
        self.save()
        return self.public(self.items[item_id])

    @staticmethod
    def public(item):
        value = {k: v for k, v in item.items() if k != "project_key"}
        if item.get("cloud"):
            value["cloud_version"] = item["cloud"]["version"]
        return value

    def rows(self):
        return [self.public(v) for v in self.items.values()]

    def remove(self, ids):
        for item in ids:
            self.items.pop(item, None)
        self.save()  # Neither snapshots nor historical jobs nor user sources are deleted.

    def enqueue(self, item_id, **options):
        item = self.items[item_id]
        return enqueue_snapshot(
            self.store,
            item["project_id"],
            item["version"],
            Path(item["source"]),
            project_key=item["project_key"],
            **options,
        )
