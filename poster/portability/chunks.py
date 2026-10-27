"""Chunked data on existing DriveFiles; publication never starts or retries a Worker."""

import hashlib
import os
import time
import base64

from poster.cloud.crypto import encrypt_output, decrypt_output
from poster.cloud.errors import CloudError
from poster.cloud.format import canonical, digest, identifier
from poster.dispatcher.security import plain_path, private_dir, restrict
from .blobs import FileBlob, CHUNK, MAX_FILE_BYTES, capacity, reader


async def upload(files, value, parent, owner, *, recipient=None, context=None, publication_id=None):
    identifier(parent)
    identifier(owner)
    size, expected = len(value), digest(value)
    if size > MAX_FILE_BYTES:
        raise CloudError("PACKAGE_LIMIT")
    key = "chunks:" + owner
    state = files.journal.get(key)
    if state is None:
        count = (size + CHUNK - 1) // CHUNK
        ids = await files.ids(count) if count else []
        state = dict(
            owner=owner,
            parent=parent,
            sha256=expected,
            size=size,
            ids=ids,
            encrypted=recipient is not None,
            recipient=recipient,
            context=context,
            created=time.time(),
            completed={},
            publication_id=publication_id,
        )
        files.journal.set(key, state)
    if any(
        state[k] != v
        for k, v in dict(
            owner=owner,
            parent=parent,
            sha256=expected,
            size=size,
            encrypted=recipient is not None,
            recipient=recipient,
            context=context,
        ).items()
    ):
        raise CloudError("DRIVE_CONFLICT")
    parts, sha, total = [], hashlib.sha256(), 0
    with reader(value) as source:
        for index, file_id in enumerate(state["ids"]):
            raw = source.read(CHUNK)
            if not raw:
                raise CloudError("INTEGRITY")
            total += len(raw)
            sha.update(raw)
            part_context = dict(context or {}, transfer=owner, index=index, size=size, whole_sha256=expected)
            prior = state["completed"].get(str(index))
            if prior:
                # Read-back is mandatory even on resumed data; no execution replay.
                stored = await files.read(file_id, limit=2 * CHUNK)
                if len(stored) != prior["size"] or digest(stored) != prior["sha256"]:
                    raise CloudError("INTEGRITY")
                if len(raw) != prior["plain_size"] or digest(raw) != prior["plain_sha256"]:
                    raise CloudError("INTEGRITY")
                part = prior
            else:
                pending = state.get("pending")
                if pending:
                    if pending["index"] != index:
                        raise CloudError("INTEGRITY")
                    stored = base64.b64decode(pending["box"], validate=True)
                else:
                    stored = encrypt_output(recipient, raw, part_context) if recipient else raw
                    state["pending"] = dict(index=index, box=base64.b64encode(stored).decode())
                    files.journal.set(key, state)
                await files.upload(
                    file_id,
                    f"part-{index:04d}.box" if recipient else f"part-{index:04d}.bin",
                    parent,
                    stored,
                    properties={"poster_transfer": owner},
                )
                part = dict(
                    id=file_id,
                    size=len(stored),
                    sha256=digest(stored),
                    plain_size=len(raw),
                    plain_sha256=digest(raw),
                )
                state["completed"][str(index)] = part
                state.pop("pending", None)
                files.journal.set(key, state)
            parts.append(part)
        if source.read(1) or total != size or sha.hexdigest() != expected:
            raise CloudError("INTEGRITY")
    # The caller commits this descriptor only after every verified part exists.
    return dict(
        format="poster.data.chunks.v1",
        owner=owner,
        size=size,
        sha256=expected,
        encrypted=recipient is not None,
        context_sha256=digest(canonical(context or {})),
        recipient_id=recipient["id"] if recipient else None,
        parts=parts,
    )


def validate(entry):
    try:
        if entry["format"] != "poster.data.chunks.v1" or type(entry["encrypted"]) is not bool:
            raise ValueError()
        identifier(entry["owner"])
        size = entry["size"]
        if type(size) is not int or not 0 <= size <= MAX_FILE_BYTES:
            raise ValueError()
        if len(entry["parts"]) != (size + CHUNK - 1) // CHUNK:
            raise ValueError()
        seen, total = set(), 0
        for index, part in enumerate(entry["parts"]):
            identifier(part["id"])
            if part["id"] in seen or part["plain_size"] != min(CHUNK, size - index * CHUNK):
                raise ValueError()
            seen.add(part["id"])
            if not 0 < part["size"] <= 2 * CHUNK:
                raise ValueError()
            for field in ("sha256", "plain_sha256"):
                if len(part[field]) != 64 or any(c not in "0123456789abcdef" for c in part[field]):
                    raise ValueError()
            total += part["plain_size"]
        if total != size or len(entry["sha256"]) != 64:
            raise ValueError()
        return entry
    except (KeyError, TypeError, ValueError) as exc:
        raise CloudError("INTEGRITY") from exc


async def download(files, entry, target, *, recipient=None, context=None):
    validate(entry)
    if entry["encrypted"] != (recipient is not None):
        raise CloudError("RESULT_UNTRUSTED")
    if entry.get("context_sha256") != digest(canonical(context or {})) or entry.get("recipient_id") != (
        recipient.public()["id"] if recipient else None
    ):
        raise CloudError("RESULT_UNTRUSTED")
    target = plain_path(target)
    private_dir(target.parent)
    capacity(target.parent, entry["size"])
    if target.exists():
        result = FileBlob.capture(target)
        if len(result) != entry["size"] or result.sha256 != entry["sha256"]:
            raise CloudError("INTEGRITY")
        return result
    partial = plain_path(target.with_name(".partial-" + digest(canonical(entry))))
    offset, sha = 0, hashlib.sha256()
    # Resume validates each existing complete plaintext part. A torn last part is truncated.
    with partial.open("r+b" if partial.exists() else "x+b") as output:
        restrict(partial)
        for index, part in enumerate(entry["parts"]):
            output.seek(offset)
            old = output.read(part["plain_size"])
            if len(old) == part["plain_size"] and digest(old) == part["plain_sha256"]:
                raw = old
            else:
                output.seek(offset)
                output.truncate()
                stored = await files.read(part["id"], limit=2 * CHUNK)
                if len(stored) != part["size"] or digest(stored) != part["sha256"]:
                    raise CloudError("INTEGRITY")
                part_context = dict(
                    context or {},
                    transfer=entry["owner"],
                    index=index,
                    size=entry["size"],
                    whole_sha256=entry["sha256"],
                )
                raw = decrypt_output(recipient, stored, part_context) if recipient else stored
                if len(raw) != part["plain_size"] or digest(raw) != part["plain_sha256"]:
                    raise CloudError("INTEGRITY")
                output.write(raw)
                output.flush()
                os.fsync(output.fileno())
            sha.update(raw)
            offset += len(raw)
        output.seek(offset)
        output.truncate()
        output.flush()
        os.fsync(output.fileno())
    if offset != entry["size"] or sha.hexdigest() != entry["sha256"]:
        partial.unlink(missing_ok=True)
        raise CloudError("INTEGRITY")
    # Create-only publication prevents a different run from overwriting a result.
    os.link(partial, target)
    partial.unlink()
    return FileBlob(target, offset, sha.hexdigest())


async def cleanup(files, owner, *, older_than=24 * 3600):
    key = "chunks:" + identifier(owner)
    state = files.journal.get(key)
    if not state or time.time() - state["created"] < older_than:
        return False
    # Explicit caller owns this incomplete upload. Never delete committed data.
    if state.get("committed"):
        return False
    if not state.get("publication_id") or await files.info(state["publication_id"]) is not None:
        return False
    for file_id in state["ids"]:
        info = await files.info(file_id)
        if info and (
            info.get("parents") != [state["parent"]]
            or info.get("properties", {}).get("poster_transfer") != owner
        ):
            raise CloudError("INTEGRITY")
        if info:
            await files.transport.api("DELETE", "files/" + identifier(file_id), accepted=(404,), retry=False)
        files.journal.set(file_id, None)
    files.journal.set(key, None)
    return True


def committed(files, entry):
    key = "chunks:" + entry["owner"]
    state = files.journal.get(key)
    if state:
        state["committed"] = True
        files.journal.set(key, state)
