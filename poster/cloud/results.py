"""Immutable detached results, encrypted outputs, and a separate desktop archive."""

from pathlib import Path
import platform
import time

from poster.dispatcher.security import plain_path, private_dir, seal, unseal
from poster.maker.storage import atomic
from .crypto import encrypt_output, decrypt_output, sign_result, verify_result
from .errors import CloudError
from .format import MAX_BYTES, canonical, digest, identifier, relative

TERMINAL = {"SUCCEEDED", "FAILED", "CANCELLED", "TIMED_OUT", "UNKNOWN", "REVIEW_REQUIRED"}
MAX_OUTPUT_BYTES = 14_000_000  # AES-GCM JSON/base64 must fit the bounded Drive blob.


def context(claim, run_id, name):
    return dict(
        mode=claim.get("mode", "DETACHED"),
        project_id=claim["project_id"],
        project_version=claim["version"],
        run_id=run_id,
        path=name,
    )


async def publish_result(library, descriptor, claim, index, identity, execution, bundle):
    """Commit only after all allowed ciphertext is uploaded and read back."""
    plan = claim["runs"][index]
    run_id, folder_id, manifest_id = plan
    if identity.public() != claim["issuer"] or execution["status"] not in TERMINAL:
        raise CloudError("RESULT_UNTRUSTED")
    files = library.files
    await files.folder(
        run_id,
        descriptor["folders"]["results"],
        file_id=folder_id,
        properties={
            "poster_result_mode": claim.get("mode", "DETACHED"),
            "poster_result_manifest": manifest_id,
        },
    )
    peer = bundle.manifest.get("result_recipient")
    outputs, total = {}, 0
    if peer is None:
        raise CloudError("RECIPIENT_REQUIRED")
    for name in bundle.manifest["outputs"]:
        relative(name)
        path = plain_path(Path(execution["workdir"]) / name) if execution.get("workdir") else None
        if path is None or not path.exists():
            continue
        if not path.is_file() or path.stat().st_size > MAX_OUTPUT_BYTES:
            raise CloudError("PACKAGE_LIMIT")
        raw = path.read_bytes()
        total += len(raw)
        if total > MAX_OUTPUT_BYTES:
            raise CloudError("PACKAGE_LIMIT")
        box = encrypt_output(peer, raw, context(claim, run_id, name))
        file_id = (await files.ids())[0]
        await files.upload(file_id, "output-" + str(len(outputs)) + ".box", folder_id, box)
        outputs[name] = dict(id=file_id, size=len(box), sha256=digest(box), plain_sha256=digest(raw))
    history = execution.get("history", [])
    current = history[0] if history else {}
    body = dict(
        format="poster.cloud.result.v1",
        mode=claim.get("mode", "DETACHED"),
        issuer=identity.public()["id"],
        project_id=claim["project_id"],
        project_version=claim["version"],
        run_id=run_id,
        claim_id=claim["id"],
        recipient=peer["id"],
        status=execution["status"],
        evidence=dict(
            os=platform.system(),
            python=platform.python_version(),
            pid=current.get("pid"),
            native_job_id=execution["id"],
            native_run_id=current.get("run_id"),
            effects_possible=bool(current.get("effects_possible")),
            report_sha256=current.get("report_sha256"),
            commit=claim.get("code_commit", "UNAVAILABLE"),
        ),
        outputs=outputs,
    )
    signed = sign_result(identity, body)
    await files.create_json(manifest_id, "committed-result.json", folder_id, signed)
    return dict(
        run_id=run_id, manifest_id=manifest_id, status=execution["status"], sha256=digest(canonical(signed))
    )


class ResultArchive:
    """Uses dispatcher.sqlite but NEVER writes managed jobs/executions/statuses."""

    def __init__(self, store):
        self.store = store

    def rows(self):
        values = []
        for row in self.store.db.execute("SELECT * FROM cloud_results ORDER BY imported DESC LIMIT 1000"):
            value = dict(row)
            safe = unseal(value.pop("manifest_cipher"))
            value.update(
                directory=safe["directory"],
                evidence=safe["manifest"]["evidence"],
                outputs=list(safe["manifest"]["outputs"]),
            )
            values.append(value)
        return values

    def find(self, issuer, run_id):
        row = self.store.db.execute(
            "SELECT manifest_cipher FROM cloud_results WHERE mode='DETACHED' AND issuer=? AND run_id=?",
            (issuer, run_id),
        ).fetchone()
        return unseal(row[0]) if row else None

    def save(self, value, safe):
        self.store.db.execute(
            "INSERT INTO cloud_results VALUES(?,?,?,?,?,?,?,?)",
            (
                "DETACHED",
                value["issuer"],
                value["run_id"],
                value["project_id"],
                value["project_version"],
                value["status"],
                time.time(),
                seal(safe),
            ),
        )

    async def import_result(self, library, claim, index, recipient):
        run_id, _, manifest_id = claim["runs"][index]
        signed = await library.files.json(manifest_id)
        value = verify_result(claim["issuer"], signed)
        bundle = await library.fetch(claim["project_id"], claim["version"])
        if any(
            (
                value.get("format") != "poster.cloud.result.v1",
                value.get("mode") != "DETACHED" or claim.get("mode") != "DETACHED",
                value.get("claim_id") != claim["id"],
                value.get("run_id") != run_id,
                value.get("project_id") != bundle.project_id,
                value.get("project_version") != bundle.version,
                value.get("status") not in TERMINAL,
                bundle.manifest.get("result_recipient") != recipient.public(),
                value.get("recipient") != recipient.public()["id"],
            )
        ):
            raise CloudError("RESULT_UNTRUSTED")
        # Public signature is bound to the pre-execution claim, not an arbitrary
        # key supplied by a result file. IDs separate modes and independent runs.
        old = self.find(value["issuer"], run_id)
        if old:
            if old["manifest"] != signed:
                raise CloudError("RESULT_UNTRUSTED")
            return dict(run_id=run_id, status=value["status"], already_imported=True)
        if len(value["outputs"]) > 128:
            raise CloudError("PACKAGE_LIMIT")
        decoded, total = {}, 0
        for name, entry in value["outputs"].items():
            relative(name)
            raw = await library.files.read(identifier(entry["id"]))
            if len(raw) != entry["size"] or digest(raw) != entry["sha256"]:
                raise CloudError("RESULT_UNTRUSTED")
            plain = decrypt_output(recipient, raw, context(claim, run_id, name))
            total += len(plain)
            if total > MAX_BYTES or digest(plain) != entry["plain_sha256"]:
                raise CloudError("RESULT_UNTRUSTED")
            decoded[name] = plain
        root = private_dir(
            self.store.root / "cloud-results" / identifier(value["issuer"]) / identifier(run_id)
        )
        for name, raw in decoded.items():
            target = plain_path(root / relative(name))
            private_dir(target.parent)
            atomic(target, raw)
        safe = dict(manifest=signed, directory=str(root))
        self.save(signed, safe)
        return dict(run_id=run_id, status=value["status"], directory=str(root), outputs=list(decoded))


class DesktopArchive(ResultArchive):
    """Qt bridge uses authenticated native IPC; SQLite stays on the Host thread."""

    def __init__(self, root):
        from types import SimpleNamespace

        self.store = SimpleNamespace(root=Path(root))

    def find(self, issuer, run_id):
        from poster.dispatcher.host import request

        return request(self.store.root, "cloud_result_find", issuer=issuer, run_id=run_id)

    def save(self, value, safe):
        from poster.dispatcher.host import request

        request(self.store.root, "cloud_result_save", value=value, safe=safe)

    def rows(self):
        from poster.dispatcher.host import request

        return request(self.store.root, "cloud_results")


async def export_managed(library, identity, entries):
    """Storage-only export of confirmed native history; never changes that history."""
    exported = []
    for entry in entries:
        origin, execution = entry["origin"], entry["execution"]
        folder = await library.find(origin["project_id"])
        if folder is None or folder["id"] != origin["folder_id"]:
            raise CloudError("INTEGRITY")
        descriptor = await library.descriptor(folder)
        bundle = await library.fetch(origin["project_id"], origin["version"])
        history = execution["history"]
        run_id = history[0]["run_id"] if history else "cancelled-" + execution["id"]
        key = "managed-export:" + bundle.version + ":" + run_id
        state = library.files.journal.get(key)
        if state and state.get("done"):
            raw = await library.files.read(state["claim"]["runs"][0][2])
            if digest(raw) != state["done"]["sha256"]:
                raise CloudError("RESULT_UNTRUSTED")
            exported.append(state["done"])
            continue
        if state is None:
            claim_id, folder_id, manifest_id = await library.files.ids(3)
            claim = dict(
                format="poster.cloud.managed-export.v1",
                mode="MANAGED_EXPORT",
                id=claim_id,
                issuer=identity.public(),
                project_id=bundle.project_id,
                version=bundle.version,
                native_snapshot=execution["version"],
                code_commit=execution.get("commit", "UNAVAILABLE"),
                runs=[[run_id, folder_id, manifest_id]],
            )
            state = dict(claim=claim)
            library.files.journal.set(key, state)
        claim = state["claim"]
        await library.files.create_json(
            claim["id"], "managed-origin.json", descriptor["folders"]["results"], claim
        )
        result = await publish_result(library, descriptor, claim, 0, identity, execution, bundle)
        library.files.journal.set(key, dict(claim=claim, done=result))
        exported.append(result)
    return exported
