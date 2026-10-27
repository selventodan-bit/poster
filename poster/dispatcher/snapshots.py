"""Content-addressed, encrypted, bounded project trees; never execute live sources."""

import base64
import hashlib
import json
import os
from pathlib import Path
import posixpath
import shutil
import uuid

from filelock import FileLock

from ..maker.actions import default_registry
from ..maker.storage import load_graph, atomic
from ..maker.variables import has_reference
from .models import DispatcherError
from .security import plain_path, private_dir, restrict, seal, unseal

MAX_FILES = 128
MAX_BYTES = 20_000_000


def digest(raw):
    from poster.portability.blobs import FileBlob

    if isinstance(raw, FileBlob):
        return raw.verify().sha256
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def reachable_graphs(manifest, files):
    """Metadata traversal of the original Project.Run dependency contract."""
    from ..maker.model import Graph

    pending, seen = [manifest["main"]], set()
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        if name not in files:
            raise DispatcherError("SNAPSHOT_INVALID")
        graph = Graph.model_validate_json(files[name])
        seen.add(name)
        yield name, graph
        for node in graph.nodes:
            if node.type == "Project.Run":
                value = node.params.get("path", "child.postergraph.json")
                if has_reference(value):
                    pending.extend(n for n in files if n.endswith(".postergraph.json"))
                elif isinstance(value, str):
                    pending.append(
                        posixpath.normpath(posixpath.join(posixpath.dirname(name), value.replace("\\", "/")))
                    )


class Snapshots:
    def __init__(self, root):
        self.root = private_dir(root / "snapshots")

    def create(self, source):
        manifest, files = self.capture(source)
        return self.publish(manifest, files)

    def capture(self, source, *, include_resources=True):
        """Capture with the existing dependency/path contract, without changing the source."""
        source = plain_path(source)
        if not source.is_file() or source.suffix != ".json":
            raise DispatcherError("PROJECT_INVALID")
        root = source.parent
        pending, files, scanned = [source], {}, 0
        # Dynamic Project.Run paths may select only graphs in this bounded tree.
        directories = [root]
        while directories:
            directory = directories.pop()
            for entry in os.scandir(directory):
                scanned += 1
                if scanned > 2048:
                    raise DispatcherError("SNAPSHOT_LIMIT")
                path = Path(entry.path)
                if entry.is_dir(follow_symlinks=False) and (
                    entry.name.startswith(".") or path == self.root.parent
                ):
                    continue
                if (
                    entry.is_symlink()
                    or getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0) & 0x400
                ):
                    if entry.is_dir() or entry.name.endswith(".postergraph.json"):
                        raise DispatcherError("PATH_FORBIDDEN")
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if len(path.relative_to(root).parts) > 8:
                        raise DispatcherError("SNAPSHOT_LIMIT")
                    directories.append(path)
                elif entry.name.endswith(".postergraph.json"):
                    pending.append(path)
        registry = default_registry()
        while pending:
            path = plain_path(pending.pop())
            if not path.is_relative_to(root):
                raise DispatcherError("PATH_FORBIDDEN")
            name = path.relative_to(root).as_posix()
            if name in files:
                continue
            try:
                before = path.read_bytes()
                graph, _ = load_graph(path)
                for node in graph.nodes:
                    registry.get(node.type, node.action_version)
                    value = node.params.get("path")
                    if node.type == "Project.Run" and isinstance(value, str) and not has_reference(value):
                        child = plain_path(path.parent / value.replace("\\", "/"))
                        if not child.is_relative_to(root):
                            raise DispatcherError("PATH_FORBIDDEN")
                        pending.append(child)
                files[name] = path.read_bytes()
                if files[name] != before:
                    raise DispatcherError("SNAPSHOT_INVALID")
                if graph.secret_file:
                    sidecar = plain_path(path.parent / graph.secret_file)
                    files[sidecar.relative_to(root).as_posix()] = sidecar.read_bytes()
                    if digest(files[sidecar.relative_to(root).as_posix()]) != graph.secret_sha256:
                        raise DispatcherError("SNAPSHOT_INVALID")
            except DispatcherError:
                raise
            except Exception as exc:
                raise DispatcherError("PROJECT_INVALID") from exc
            if len(files) > MAX_FILES or sum(map(len, files.values())) > MAX_BYTES:
                raise DispatcherError("SNAPSHOT_LIMIT")
        graph, _ = load_graph(source)
        # Detect a source edit during capture instead of assigning a misleading identity.
        if source.read_bytes() != files[source.name]:
            raise DispatcherError("SNAPSHOT_INVALID")
        resources = graph.metadata.get("poster.resources", {}) if include_resources else {}
        if not isinstance(resources, dict):
            raise DispatcherError("SNAPSHOT_INVALID")
        if resources:
            from poster.cloud.package import public_resource
            from poster.portability.blobs import FileBlob, MAX_FILE_BYTES, MAX_BUNDLE_BYTES

            for name, expected in resources.items():
                try:
                    public_resource(name)
                    resource = plain_path(root / name)
                    if not resource.is_relative_to(root) or not resource.is_file():
                        raise ValueError()
                    if resource.stat().st_size > MAX_FILE_BYTES:
                        raise ValueError()
                    raw = (
                        FileBlob.capture(resource)
                        if resource.stat().st_size > 1024 * 1024
                        else resource.read_bytes()
                    )
                    if digest(raw) != expected or name in files:
                        raise ValueError()
                    files[name] = raw
                except Exception as exc:
                    raise DispatcherError("SNAPSHOT_INVALID") from exc
            if len(files) > MAX_FILES or sum(map(len, files.values())) > MAX_BUNDLE_BYTES:
                raise DispatcherError("SNAPSHOT_LIMIT")
        manifest = {
            "version": 2 if sum(map(len, files.values())) > MAX_BYTES else 1,
            "main": source.name,
            "project_id": graph.id,
            "files": {name: digest(raw) for name, raw in sorted(files.items())},
        }
        return manifest, files

    def publish(self, manifest, files):
        from poster.portability.blobs import FileBlob

        # Keep the original 5C/protocol-1 bundle contract for bounded snapshots.
        if manifest["version"] == 1:
            files = {
                n: v.verify().path.read_bytes() if isinstance(v, FileBlob) else v for n, v in files.items()
            }
        version = digest(canonical(manifest))
        target = self.root / version
        with FileLock(str(self.root / ".publication.lock")):
            if not target.exists():
                staging = private_dir(self.root / (".pending_" + uuid.uuid4().hex))
                try:
                    from poster.portability.blobs import FileBlob, copy_blob

                    external = {n: v for n, v in files.items() if isinstance(v, FileBlob)}
                    for name, blob in external.items():
                        copy_blob(blob, staging / "data" / name, readonly=True)
                    atomic(
                        staging / "bundle.enc",
                        seal(
                            {
                                "files": {
                                    n: base64.b64encode(v).decode()
                                    for n, v in files.items()
                                    if n not in external
                                },
                                "external": {
                                    n: dict(size=len(v), sha256=v.sha256) for n, v in external.items()
                                },
                            }
                        ),
                    )
                    atomic(staging / "manifest.json", canonical(manifest))
                    restrict(staging / "bundle.enc")
                    restrict(staging / "manifest.json")
                    staging.rename(target)  # Publish the complete immutable tree together.
                finally:
                    if staging.exists():
                        shutil.rmtree(staging)
            self.read(version)
        return manifest["project_id"], version

    def read(self, version):
        try:
            if len(version) != 64 or any(c not in "0123456789abcdef" for c in version):
                raise ValueError()
            target = plain_path(self.root / version)
            raw = plain_path(target / "manifest.json").read_bytes()
            if len(raw) > 100_000 or digest(raw) != version:
                raise ValueError()
            manifest = json.loads(raw)
            bundle_path = plain_path(target / "bundle.enc")
            if bundle_path.stat().st_size > 50_000_000:
                raise ValueError()
            envelope = unseal(bundle_path.read_bytes())
            encoded, external = envelope["files"], envelope.get("external", {})
            if (
                set(encoded) | set(external) != set(manifest["files"])
                or set(encoded) & set(external)
                or len(manifest["files"]) > MAX_FILES
            ):
                raise ValueError()
            files = {n: base64.b64decode(v, validate=True) for n, v in encoded.items()}
            from poster.portability.blobs import FileBlob, MAX_BUNDLE_BYTES
            from poster.cloud.format import relative

            for name, entry in external.items():
                relative(name)
                blob = FileBlob.capture(target / "data" / name)
                if len(blob) != entry["size"] or blob.sha256 != entry["sha256"]:
                    raise ValueError()
                files[name] = blob
            if sum(map(len, files.values())) > (MAX_BYTES if manifest["version"] == 1 else MAX_BUNDLE_BYTES):
                raise ValueError()
            for name, content in files.items():
                path = Path(name)
                if (
                    path.is_absolute()
                    or ".." in path.parts
                    or "\\" in name
                    or ":" in name
                    or digest(content) != manifest["files"][name]
                ):
                    raise ValueError()
            if manifest["main"] not in files or manifest["version"] not in {1, 2}:
                raise ValueError()
            return manifest, files
        except DispatcherError as exc:
            if exc.code == "KEY_INVALID":
                raise
            raise DispatcherError("SNAPSHOT_INVALID") from exc
        except Exception as exc:
            raise DispatcherError("SNAPSHOT_INVALID") from exc

    def materialize(self, version, destination):
        manifest, files = self.read(version)
        destination = private_dir(destination)
        if any(destination.iterdir()):
            raise DispatcherError("SNAPSHOT_INVALID")
        for name, raw in files.items():
            from poster.portability.blobs import copy_blob

            target = plain_path(destination / name)
            private_dir(target.parent)
            copy_blob(raw, target, readonly=True)
        return destination / manifest["main"]
