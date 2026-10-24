"""Atomic graph files and encrypted secret references, using the existing key service."""

import base64
import hashlib
import json
import os
from pathlib import Path

from ..auth import decrypt_bytes, encrypt_session
from .model import Graph, GraphError, identity, graph_secret_refs

MAX_FILE = 10_000_000


class Vault:
    def __init__(self, env_allowed=()):
        self.tokens = {}
        self.env_allowed = set(env_allowed)

    def put(self, value, ref=None):
        if not isinstance(value, str) or len(value) > 200_000:
            raise GraphError("SECRET_INVALID")
        return self.put_value(value, ref)

    def put_value(self, value, ref=None):
        try:
            raw = json.dumps(value, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise GraphError("SECRET_INVALID") from exc
        if len(raw.encode("utf-8")) > 1_000_000:
            raise GraphError("SECRET_INVALID")
        ref = ref or "secret_" + identity()
        self.tokens[ref] = encrypt_session({"value": value})
        return {"secret_ref": ref}

    def get(self, ref):
        if ref.startswith("env:") and ref not in self.tokens:
            if ref[4:] not in self.env_allowed:
                raise GraphError("SECRET_MISSING")
            value = os.environ.get(ref[4:])
            if value is None:
                raise GraphError("SECRET_MISSING")
            return value
        try:
            return decrypt_bytes(self.tokens[ref])["value"]
        except Exception as exc:
            raise GraphError("SECRET_MISSING") from exc

    def resolve(self, value):
        return (
            self.get(value["secret_ref"])
            if isinstance(value, dict) and set(value) == {"secret_ref"}
            else value
        )


def read_bounded(path):
    if path.is_symlink() or path.stat().st_size > MAX_FILE:
        raise GraphError("FILE_INVALID")
    return path.read_bytes()


def atomic(path, raw):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("." + path.name + "." + identity() + ".tmp")
    try:
        temporary.write_bytes(raw)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def save_graph(path, graph, vault):
    path = Path(path)
    if path.suffix != ".json":
        raise GraphError("FILE_INVALID")
    if path.exists():
        try:
            if json.loads(read_bounded(path)).get("format") != "poster.maker.graph":
                raise GraphError("FILE_INVALID")
        except (ValueError, AttributeError) as exc:
            raise GraphError("FILE_INVALID") from exc
    saved = graph.validate_structure().model_copy(deep=True)
    refs = graph_secret_refs(saved)
    tokens = {k: base64.b64encode(v).decode("ascii") for k, v in vault.tokens.items() if k in refs}
    if tokens:
        raw = json.dumps(
            {"format": "poster.maker.secrets", "version": 1, "project": saved.id, "tokens": tokens},
            sort_keys=True,
        ).encode("utf-8")
        digest = hashlib.sha256(raw).hexdigest()
        if len(raw) > MAX_FILE:
            raise GraphError("FILE_TOO_LARGE")
        filename = path.stem + "." + digest[:16] + ".secrets.enc"
        atomic(path.parent / filename, raw)
        saved.secret_file, saved.secret_sha256 = filename, digest
    else:
        saved.secret_file = saved.secret_sha256 = None
    raw = saved.model_dump_json(indent=2).encode("utf-8")
    if len(raw) > MAX_FILE:
        raise GraphError("FILE_TOO_LARGE")
    if path.exists():
        old = read_bounded(path)
        if json.loads(old).get("schema_version") == 1:
            backup = path.with_name(path.name + ".v1.bak")
            if not backup.exists():
                atomic(backup, old)
    atomic(path, raw)  # Commit the graph last; the previous encrypted bundle stays recoverable.
    return saved


def load_graph(path):
    path = Path(path)
    try:
        graph = Graph.model_validate_json(read_bounded(path))
        vault = Vault()
        if graph.secret_file:
            name = graph.secret_file
            if Path(name).name != name or "/" in name or "\\" in name or not name.endswith(".secrets.enc"):
                raise GraphError("SECRET_FILE_INVALID")
            raw = read_bounded(path.parent / name)
            if hashlib.sha256(raw).hexdigest() != graph.secret_sha256:
                raise GraphError("SECRET_FILE_INVALID")
            data = json.loads(raw)
            if (
                data.get("format") != "poster.maker.secrets"
                or data.get("version") != 1
                or data.get("project") != graph.id
            ):
                raise GraphError("SECRET_FILE_INVALID")
            vault.tokens = {k: base64.b64decode(v, validate=True) for k, v in data["tokens"].items()}
        return graph, vault
    except GraphError:
        raise
    except Exception as exc:
        # ValidationError can contain parameter values. Never expose it in UI/CLI logs.
        raise GraphError("FILE_INVALID") from exc
