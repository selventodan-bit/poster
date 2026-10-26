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


def context(claim, run_id, name):
    return dict(
        mode="DETACHED",
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
    await files.folder(run_id, descriptor["folders"]["results"], file_id=folder_id)
    peer = bundle.manifest.get("result_recipient")
    outputs, total = {}, 0
    if peer is None:
        raise CloudError("RECIPIENT_REQUIRED")
    for name in bundle.manifest["outputs"]:
        relative(name)
        path = plain_path(Path(execution["workdir"]) / name) if execution.get("workdir") else None
        if path is None or not path.exists():
            continue
        if not path.is_file() or path.stat().st_size > MAX_BYTES:
            raise CloudError("PACKAGE_LIMIT")
        raw = path.read_bytes()
        total += len(raw)
        if total > MAX_BYTES:
            raise CloudError("PACKAGE_LIMIT")
        box = encrypt_output(peer, raw, context(claim, run_id, name))
        file_id = (await files.ids())[0]
        await files.upload(file_id, "output-" + str(len(outputs)) + ".box", folder_id, box)
        outputs[name] = dict(id=file_id, size=len(box), sha256=digest(box), plain_sha256=digest(raw))
    history = execution.get("history", [])
    current = history[0] if history else {}
    body = dict(
        format="poster.cloud.result.v1",
        mode="DETACHED",
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
        return [
            dict(row)
            for row in self.store.db.execute(
                "SELECT mode,issuer,run_id,project_id,project_version,status,imported FROM cloud_results ORDER BY imported DESC LIMIT 1000"
            )
        ]

    async def import_result(self, library, claim, index, recipient):
        run_id, _, manifest_id = claim["runs"][index]
        signed = await library.files.json(manifest_id)
        value = verify_result(claim["issuer"], signed)
        bundle = await library.fetch(claim["project_id"], claim["version"])
        if any(
            (
                value.get("format") != "poster.cloud.result.v1",
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
        old = self.store.db.execute(
            "SELECT manifest_cipher FROM cloud_results WHERE mode=? AND issuer=? AND run_id=?",
            ("DETACHED", value["issuer"], run_id),
        ).fetchone()
        if old:
            if unseal(old[0])["manifest"] != signed:
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
        self.store.db.execute(
            "INSERT INTO cloud_results VALUES(?,?,?,?,?,?,?,?)",
            (
                "DETACHED",
                value["issuer"],
                run_id,
                bundle.project_id,
                bundle.version,
                value["status"],
                time.time(),
                seal(safe),
            ),
        )
        return dict(run_id=run_id, status=value["status"], directory=str(root), outputs=list(decoded))
