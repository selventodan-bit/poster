"""Explicit resource/directory preview and conservative dependency diagnostics."""

import os
from pathlib import Path, PureWindowsPath
import stat
import zipfile

from poster.cloud.errors import CloudError
from poster.cloud.format import relative
from poster.cloud.package import public_resource
from poster.dispatcher.security import plain_path
from poster.maker.variables import has_reference
from .blobs import FileBlob, MAX_BUNDLE_BYTES, MAX_FILE_BYTES


def archive_check(path):
    if path.suffix.lower() != ".zip":
        return
    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            if len(entries) > 128 or sum(e.file_size for e in entries) > MAX_BUNDLE_BYTES:
                raise CloudError("PACKAGE_LIMIT")
            for entry in entries:
                name = entry.filename.rstrip("/")
                public_resource(name)
                if (
                    stat.S_ISLNK(entry.external_attr >> 16)
                    or entry.flag_bits & 1
                    or entry.file_size > MAX_FILE_BYTES
                    or entry.file_size > max(1, entry.compress_size) * 100
                ):
                    raise CloudError("PACKAGE_LIMIT")
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        raise CloudError("INTEGRITY") from exc


def preview(project_root, selections):
    root = plain_path(project_root)
    entries, total, scanned = {}, 0, 0
    for value in selections:
        selected = plain_path(value)
        if selected in {Path.home().resolve(), Path(selected.anchor)}:
            raise CloudError("PRIVATE_RESOURCE")
        # Inspect the explicitly selected directory itself, not just its children.
        public_resource(selected.name)
        pending = [selected]
        while pending:
            path = plain_path(pending.pop())
            scanned += 1
            if scanned > 4096:
                raise CloudError("PACKAGE_LIMIT")
            if path.is_relative_to(root):
                name = path.relative_to(root).as_posix()
            elif selected.is_dir():
                name = "resources/" + selected.name + "/" + path.relative_to(selected).as_posix()
            else:
                name = "resources/" + path.name
            if path == selected and path.is_dir() and not path.is_relative_to(root):
                name = "resources/" + selected.name
            if path == root:
                # Project-root selection is deliberately explicit; never include its graph/sidecars.
                raise CloudError("PRIVATE_RESOURCE")
            public_resource(name)
            if len(Path(name).parts) > 10:
                raise CloudError("PACKAGE_LIMIT")
            if path.is_dir():
                for entry in os.scandir(path):
                    # Never silently traverse a rejected link/private directory.
                    pending.append(Path(entry.path))
                continue
            if not path.is_file():
                raise CloudError("PATH_FORBIDDEN")
            archive_check(path)
            blob = FileBlob.capture(path)
            if name.casefold() in entries:
                if entries[name.casefold()]["source"] == str(path):
                    continue
                raise CloudError("INTEGRITY")
            total += len(blob)
            if len(entries) >= 128 or total > MAX_BUNDLE_BYTES:
                raise CloudError("PACKAGE_LIMIT")
            entries[name.casefold()] = dict(name=name, source=str(path), size=len(blob), sha256=blob.sha256)
    return dict(
        files=sorted(entries.values(), key=lambda e: e["name"]),
        total_bytes=total,
        limits=dict(files=128, file_bytes=MAX_FILE_BYTES, total_bytes=MAX_BUNDLE_BYTES),
    )


def dependencies(graph, resources, *, target="linux"):
    available = set(resources)
    generated, issues = set(), []
    reads = {
        "Files.ReadText": ("path",),
        "Files.Copy": ("source",),
        "Files.Move": ("source",),
        "CSV.Read": ("path",),
        "Excel.Read": ("path",),
        "JSON.ReadFile": ("path",),
    }
    writes = {
        "Files.WriteText": "path",
        "Files.AppendText": "path",
        "Files.Copy": "destination",
        "Files.Move": "destination",
        "HTTP.Download": "download_path",
        "CSV.Write": "path",
        "Excel.Write": "path",
        "JSON.WriteFile": "path",
    }
    # A generated path is a conditional promise, not proof it exists at runtime.
    for node in graph.nodes:
        paths = [(field, node.params.get(field)) for field in reads.get(node.type, ())]
        if node.type.startswith("HTTP."):
            paths.extend(("files", value) for value in node.params.get("files", {}).values())
        for field, value in paths:
            if not isinstance(value, str) or has_reference(value):
                issues.append(dict(node_id=node.id, field=field, code="DYNAMIC_DEPENDENCY"))
                continue
            if PureWindowsPath(value).drive or Path(value).is_absolute():
                issues.append(dict(node_id=node.id, field=field, code="ABSOLUTE_PATH"))
            else:
                try:
                    relative(value)
                except CloudError:
                    issues.append(dict(node_id=node.id, field=field, code="PATH_FORBIDDEN"))
                    continue
                if value not in available:
                    issues.append(
                        dict(
                            node_id=node.id,
                            field=field,
                            code="GENERATED_DEPENDENCY" if value in generated else "MISSING_DEPENDENCY",
                        )
                    )
        for field in ("path", "source", "destination", "directory", "download_path"):
            value = node.params.get(field)
            if target != "windows" and isinstance(value, str) and PureWindowsPath(value).drive:
                if not any(i["node_id"] == node.id and i["field"] == field for i in issues):
                    issues.append(dict(node_id=node.id, field=field, code="ABSOLUTE_PATH"))
        if node.type in writes and isinstance(node.params.get(writes[node.type]), str):
            generated.add(node.params[writes[node.type]])
    return issues
