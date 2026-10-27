"""Project catalog and immutable commit chains, backed by real Drive IDs.

Two writers contend on the SAME server-generated commit ID. Only Drive's atomic
create can win. No filename lock, ETag assumption, or Drive-backed managed queue.
"""

import asyncio
import time
from pathlib import PurePosixPath

from .drive_files import FOLDER
from .errors import CloudError
from .format import canonical, digest, identifier, metadata, version_id


class Library:
    def __init__(self, files, *, root_id=None):
        self.files, self.root_id = files, root_id
        self._root_lock = asyncio.Lock()

    async def root(self, *, create=False):
        async with self._root_lock:
            if self.root_id:
                info = await self.files.info(self.root_id)
                if (
                    not info
                    or info.get("mimeType") != FOLDER
                    or info.get("properties", {}).get("poster_root") != "v1"
                ):
                    raise CloudError("INTEGRITY")
                return self.root_id
            roots = [f async for f in self.files.list(properties={"poster_root": "v1"}, folders=True)]
            if len(roots) > 1:
                raise CloudError("DRIVE_AMBIGUOUS")
            if roots:
                self.root_id = roots[0]["id"]
            elif create:
                reserved = self.files.journal.get("root") or (await self.files.ids())[0]
                self.files.journal.set("root", reserved)
                self.root_id = (
                    await self.files.folder("Poster3", file_id=reserved, properties={"poster_root": "v1"})
                )["id"]
            return self.root_id

    async def projects(self):
        root = await self.root()
        if not root:
            return
        async for folder in self.files.list(root, folders=True):
            properties = folder.get("properties", {})
            if "poster_project" not in properties:
                continue
            try:
                descriptor = await self.descriptor(folder)
                if not descriptor:
                    continue
                history, _ = await self.versions(descriptor)
                if not history:
                    continue
                latest = history[-1]
                yield dict(
                    project_id=descriptor["project_id"],
                    name=folder["name"],
                    folder_id=folder["id"],
                    version=latest["version"],
                    commit_id=latest["id"],
                    url=self.folder_url(folder["id"]),
                    status="READY",
                )
            except (ValueError, KeyError):
                yield dict(
                    project_id=properties.get("poster_project", ""),
                    name=folder.get("name", ""),
                    folder_id=folder["id"],
                    status="INVALID",
                    error="INTEGRITY",
                )

    @staticmethod
    def folder_url(folder_id):
        return "https://drive.google.com/drive/folders/" + identifier(folder_id)

    async def find(self, project_id):
        root = await self.root()
        if not root:
            return None
        matches = [
            f
            async for f in self.files.list(
                root, properties={"poster_project": identifier(project_id)}, folders=True
            )
        ]
        if len(matches) > 1:
            raise CloudError("DRIVE_AMBIGUOUS")
        return matches[0] if matches else None

    async def descriptor(self, folder):
        properties = folder.get("properties", {})
        file_id = properties.get("poster_descriptor")
        if not file_id or await self.files.info(file_id) is None:
            return None
        value = await self.files.json(file_id)
        if (
            value.get("format") != "poster.drive.project.v1"
            or value.get("project_id") != properties.get("poster_project")
            or value.get("folder_id") != folder["id"]
        ):
            raise CloudError("INTEGRITY")
        for field in ("version_slot", "run_slot"):
            identifier(value[field])
        for role in ("versions", "resources", "results"):
            identifier(value["folders"][role])
        return value

    async def versions(self, descriptor):
        cursor, previous, seen, rows = descriptor["version_slot"], None, set(), []
        while True:
            if cursor in seen or len(seen) > 100_000:
                raise CloudError("INTEGRITY")
            seen.add(cursor)
            info = await self.files.info(cursor)
            if info is None:
                return rows, cursor
            value = await self.files.json(cursor)
            if (
                value.get("format") != "poster.drive.version.v1"
                or value.get("project_id") != descriptor["project_id"]
                or value.get("previous") != previous
            ):
                raise CloudError("INTEGRITY")
            version_id(value["version"])
            version_id(value["manifest_sha256"])
            identifier(value["manifest_id"])
            value["id"] = cursor
            rows.append(value)
            cursor, previous = identifier(value["next_slot"]), value["version"]

    async def _create_project(self, bundle, name, recipient):
        root = await self.root(create=True)
        key = "project:" + bundle.project_id
        state = self.files.journal.get(key)
        if not state:
            values = await self.files.ids(8)
            state = dict(
                zip(
                    (
                        "folder_id",
                        "descriptor_id",
                        "versions",
                        "resources",
                        "results",
                        "version_slot",
                        "run_slot",
                        "first_graph",
                    ),
                    values,
                    strict=True,
                )
            )
            self.files.journal.set(key, state)
        folder = await self.files.folder(
            name,
            root,
            file_id=state["folder_id"],
            properties={"poster_project": bundle.project_id, "poster_descriptor": state["descriptor_id"]},
        )
        for role in ("versions", "resources", "results"):
            await self.files.folder(role, folder["id"], file_id=state[role])
        descriptor = dict(
            format="poster.drive.project.v1",
            project_id=bundle.project_id,
            folder_id=folder["id"],
            version_slot=state["version_slot"],
            run_slot=state["run_slot"],
            folders={n: state[n] for n in ("versions", "resources", "results")},
            result_recipient=recipient,
        )
        await self.files.upload(
            state["first_graph"],
            "project.postergraph.json",
            folder["id"],
            bundle.files[bundle.manifest["main"]],
            mime="application/json",
        )
        await self.files.create_json(state["descriptor_id"], ".poster-project.json", folder["id"], descriptor)
        matches = [
            f
            async for f in self.files.list(
                root, properties={"poster_project": bundle.project_id}, folders=True
            )
        ]
        if len(matches) != 1:
            raise CloudError("DRIVE_AMBIGUOUS")
        self.files.journal.set(key, None)
        return folder, descriptor

    async def publish(self, bundle, *, name, expected_version=None, recipient=None):
        bundle.verify()
        project_name = name
        folder = await self.find(bundle.project_id)
        if folder:
            descriptor = await self.descriptor(folder)
            if not descriptor:
                # Only the original publisher may resume its own private IDs.
                state = self.files.journal.get("project:" + bundle.project_id)
                if not state or state["folder_id"] != folder["id"]:
                    raise CloudError("DRIVE_AMBIGUOUS")
                folder, descriptor = await self._create_project(bundle, name, recipient)
        else:
            folder, descriptor = await self._create_project(bundle, name, recipient)
        history, slot = await self.versions(descriptor)
        latest = history[-1]["version"] if history else None
        key = "publication:" + bundle.project_id + ":" + bundle.version
        if latest == bundle.version:
            self.files.journal.set(key, None)
            self.files.journal.set(key + ":directories", None)
            return dict(
                project_id=bundle.project_id,
                folder_id=folder["id"],
                version=latest,
                commit_id=history[-1]["id"],
                unchanged=True,
            )
        if latest != expected_version:
            raise CloudError("DRIVE_CONFLICT")
        state = self.files.journal.get(key)
        if not state:
            values = iter(await self.files.ids(4 + len(bundle.files)))
            state = dict(
                version_folder=next(values),
                resource_folder=next(values),
                manifest_id=next(values),
                next_slot=next(values),
                slot=slot,
                created=time.time(),
                files={n: next(values) for n in sorted(bundle.files)},
            )
            self.files.journal.set(key, state)
        if state["slot"] != slot:
            # Only an explicit fresh expected_version can reach this branch.
            # Reuse immutable uploaded bytes, publish to the new empty slot.
            state.update(slot=slot, next_slot=(await self.files.ids())[0], created=time.time())
            self.files.journal.set(key, state)
        await self.files.folder(
            bundle.version, descriptor["folders"]["versions"], file_id=state["version_folder"]
        )
        await self.files.folder(
            bundle.version, descriptor["folders"]["resources"], file_id=state["resource_folder"]
        )
        graph_files = set(bundle.manifest["graphs"])
        parents = {"graphs": {".": state["version_folder"]}, "resources": {".": state["resource_folder"]}}
        # A directory ID can belong to only one category; reserve separate IDs
        # where graphs and resources happen to share the same relative directory.
        directory_ids = self.files.journal.get(key + ":directories") or {}
        entries = {}
        for name in sorted(bundle.files):
            category = "graphs" if name in graph_files else "resources"
            path = PurePosixPath(name)
            prefix = PurePosixPath(".")
            for part in path.parts[:-1]:
                previous = str(prefix)
                prefix = prefix / part
                current = str(prefix)
                if current not in parents[category]:
                    lookup = category + ":" + current
                    if lookup not in directory_ids:
                        directory_ids[lookup] = (await self.files.ids())[0]
                        self.files.journal.set(key + ":directories", directory_ids)
                    await self.files.folder(part, parents[category][previous], file_id=directory_ids[lookup])
                    parents[category][current] = directory_ids[lookup]
            from poster.portability.blobs import FileBlob
            from poster.portability.chunks import upload as upload_chunks

            blob = bundle.files[name]
            if bundle.manifest["schema_version"] == 2 and name not in graph_files:
                owner = digest(canonical(dict(project=bundle.project_id, version=bundle.version, path=name)))
                entries[name] = await upload_chunks(
                    self.files,
                    blob,
                    parents[category][str(path.parent)],
                    owner,
                    publication_id=state["manifest_id"],
                )
            else:
                if isinstance(blob, FileBlob):
                    blob = blob.verify().path.read_bytes()  # 5C bounded <=20 MB compatibility.
                await self.files.upload(
                    state["files"][name],
                    path.name,
                    parents[category][str(path.parent)],
                    blob,
                    mime="application/json" if name in graph_files else "application/octet-stream",
                )
                entries[name] = dict(id=state["files"][name], sha256=digest(blob), size=len(blob))
        transport_manifest = dict(
            format="poster.drive.bundle.v1",
            version=bundle.version,
            manifest=bundle.manifest,
            files=entries,
        )
        await self.files.create_json(
            state["manifest_id"], "manifest.json", state["version_folder"], transport_manifest
        )
        commit = dict(
            format="poster.drive.version.v1",
            project_id=bundle.project_id,
            version=bundle.version,
            previous=latest,
            next_slot=state["next_slot"],
            manifest_id=state["manifest_id"],
            manifest_sha256=digest(canonical(transport_manifest)),
            created=state["created"],
            name=project_name,
        )
        await self.files.create_json(slot, "commit.json", descriptor["folders"]["versions"], commit)
        from poster.portability.chunks import committed

        for entry in entries.values():
            if entry.get("format") == "poster.data.chunks.v1":
                committed(self.files, entry)
        self.files.journal.set(key, None)
        self.files.journal.set(key + ":directories", None)
        return dict(
            project_id=bundle.project_id,
            folder_id=folder["id"],
            version=bundle.version,
            commit_id=slot,
            unchanged=False,
        )

    async def fetch(self, project_id, version=None):
        from .package import PortableBundle

        folder = await self.find(project_id)
        if not folder:
            raise CloudError("INTEGRITY")
        descriptor = await self.descriptor(folder)
        if not descriptor:
            raise CloudError("INTEGRITY")
        versions, _ = await self.versions(descriptor)
        selected = next((v for v in reversed(versions) if version is None or v["version"] == version), None)
        if not selected:
            raise CloudError("INTEGRITY")
        raw = await self.files.read(selected["manifest_id"])
        if digest(raw) != selected["manifest_sha256"]:
            raise CloudError("INTEGRITY")
        value = metadata(raw)
        if value.get("format") != "poster.drive.bundle.v1" or value.get("version") != selected["version"]:
            raise CloudError("INTEGRITY")
        blobs = {}
        from .format import MAX_BYTES, MAX_FILES, relative
        from poster.portability.blobs import MAX_BUNDLE_BYTES
        from poster.portability.chunks import download

        if not 1 <= len(value["files"]) <= MAX_FILES:
            raise CloudError("PACKAGE_LIMIT")
        limit = MAX_BUNDLE_BYTES if value["manifest"].get("schema_version") == 2 else MAX_BYTES
        if (
            any(type(e.get("size")) is not int or e["size"] < 0 for e in value["files"].values())
            or sum(e["size"] for e in value["files"].values()) > limit
        ):
            raise CloudError("PACKAGE_LIMIT")
        for name, entry in value["files"].items():
            relative(name)
            if entry.get("format") == "poster.data.chunks.v1":
                if entry["encrypted"]:
                    raise CloudError("INTEGRITY")
                from poster.dispatcher.security import private_dir

                cache = private_dir(self.files.journal.path.parent / "resources" / selected["version"])
                blob = await download(self.files, entry, cache / digest(name.encode()))
            else:
                blob = await self.files.read(entry["id"])
            if len(blob) != entry["size"] or digest(blob) != entry["sha256"]:
                raise CloudError("INTEGRITY")
            blobs[name] = blob
            limit = MAX_BUNDLE_BYTES if value["manifest"].get("schema_version") == 2 else MAX_BYTES
            if sum(map(len, blobs.values())) > limit:
                raise CloudError("PACKAGE_LIMIT")
        bundle = PortableBundle(value["manifest"], blobs).verify()
        if bundle.version != selected["version"] or bundle.project_id != project_id:
            raise CloudError("INTEGRITY")
        return bundle

    async def rename(self, project_id, name):
        if not isinstance(name, str) or not name.strip() or len(name) > 200:
            raise CloudError("INPUT_INVALID")
        folder = await self.find(project_id)
        if not folder:
            raise CloudError("INTEGRITY")
        await self.files.transport.api(
            "PATCH",
            "files/" + folder["id"],
            retry=False,
            params={"supportsAllDrives": "true"},
            json={"name": name},
            max_bytes=200_000,
        )
        return dict(project_id=project_id, name=name, folder_id=folder["id"])
