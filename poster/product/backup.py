"""Explicit, encrypted offline product snapshots. No graph or job is executed."""

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import shutil
import sqlite3
import tempfile
import time
import uuid
import zipfile

from poster.dispatcher.models import DispatcherError
from poster.dispatcher.security import plain_path, private_dir, restrict
from . import crypto
from .locking import offline

MAX_FILES = 20000
MAX_FILE = 4 * 1024**3
MAX_MANIFEST = 16 * 1024**2
FORMAT = "poster.product.backup.v1"


def default_roots(maker):
    from poster.cloud.auth import default_root
    from poster.dispatcher.workspace import workspace_path
    from .paths import product_home

    candidates = dict(
        maker=plain_path(maker),
        dispatcher=workspace_path(),
        cloud=plain_path(default_root()),
        legacy_key=plain_path(Path.home() / ".poster/session.key"),
        location=product_home() / "maker-location.json",
    )
    # A containing selected workspace already owns the complete child scope.
    return {
        k: p
        for k, p in candidates.items()
        if not any(p != other and p.is_relative_to(other) for other in candidates.values())
    }


def binding():
    if os.name == "nt":
        import winreg
        from poster.dispatcher.security import windows_sid

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Cryptography",
            0,
            winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
        ) as key:
            machine = winreg.QueryValueEx(key, "MachineGuid")[0]
        principal = windows_sid()
    else:
        machine = (
            Path("/etc/machine-id").read_text().strip()
            if Path("/etc/machine-id").is_file()
            else platform.node()
        )
        principal = str(os.getuid())
    return hashlib.sha256((os.name + "\0" + machine + "\0" + principal).encode()).hexdigest()


def roots_checked(roots):
    result = {}
    if not isinstance(roots, dict) or not 1 <= len(roots) <= 32:
        raise DispatcherError("BACKUP_SCOPE")
    for name, path in roots.items():
        if (
            not isinstance(name, str)
            or not name
            or len(name) > 40
            or any(c not in "abcdefghijklmnopqrstuvwxyz_0123456789" for c in name)
        ):
            raise DispatcherError("BACKUP_SCOPE")
        p = plain_path(path)
        if p == Path(p.anchor) or p == Path.home().resolve():
            raise DispatcherError("BACKUP_SCOPE")
        result[name] = p
    paths = list(result.values())
    if len(set(paths)) != len(paths) or any(a != b and a.is_relative_to(b) for a in paths for b in paths):
        raise DispatcherError("BACKUP_SCOPE")
    return result


def skipped(relative):
    return (
        any(
            p in {"__pycache__", "browser-cache", "browsers", "tmp", ".poster-product-locks"}
            for p in relative.parts
        )
        or relative.name.endswith((".lock", "-wal", "-shm", ".partial", ".sock"))
        or relative.name in {"service.json", "startup-error.json", "ipc.key"}
    )


def inventory(roots):
    roots = roots_checked(roots)
    scopes, files, dirs = [], [], []
    total = 0
    for name, root in roots.items():
        if not root.exists():
            continue
        scopes.append(dict(name=name, path=str(root), kind="directory" if root.is_dir() else "file"))
        paths = walk(root) if root.is_dir() else [root]
        for path in paths:
            rel = path.relative_to(root) if path != root else Path("")
            if skipped(rel):
                continue
            path = plain_path(path)
            if len(files) + len(dirs) >= MAX_FILES:
                raise DispatcherError("BACKUP_LIMIT")
            if path != root:
                relative(rel.as_posix())
            if path.is_dir():
                dirs.append(dict(scope=name, path=rel.as_posix()))
                continue
            if not path.is_file():
                raise DispatcherError("BACKUP_SCOPE")
            size = path.stat().st_size
            if path.stat().st_nlink > 1:
                raise DispatcherError("BACKUP_SCOPE")
            wal = Path(str(path) + "-wal")
            if wal.exists():
                size += plain_path(wal).stat().st_size
            if size > MAX_FILE:
                raise DispatcherError("BACKUP_LIMIT")
            total += size
            if total > crypto.MAX_BYTES or len(files) + len(dirs) >= MAX_FILES:
                raise DispatcherError("BACKUP_LIMIT")
            files.append(
                dict(scope=name, path=rel.as_posix() if path != root else "", source=path, size=size)
            )
    return dict(scopes=scopes, files=files, directories=dirs, bytes=total)


def walk(root):
    pending = [root]
    count = 0
    while pending:
        with os.scandir(pending.pop()) as entries:
            for entry in entries:
                path = Path(entry.path)
                if skipped(path.relative_to(root)):
                    continue
                count += 1
                if count > MAX_FILES:
                    raise DispatcherError("BACKUP_LIMIT")
                path = plain_path(path)
                yield path
                if path.is_dir():
                    pending.append(path)


def space(parent, size):
    if shutil.disk_usage(parent).free < size + 64 * 1024**2:
        raise DispatcherError("BACKUP_LIMIT")


def digest(path):
    with plain_path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def sqlite_snapshot(source, target):
    with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as src, sqlite3.connect(target) as dst:
        src.backup(dst)
        if (
            dst.execute("PRAGMA integrity_check").fetchone()[0] != "ok"
            or dst.execute("PRAGMA foreign_key_check").fetchall()
        ):
            raise DispatcherError("BACKUP_INVALID")
    restrict(target)


def create(roots, destination, password, *, confirmed=False):
    if confirmed is not True:
        raise DispatcherError("BACKUP_PASSPHRASE")
    roots = roots_checked(roots)
    destination = plain_path(destination)
    if destination.exists() or any(destination == p or destination.is_relative_to(p) for p in roots.values()):
        raise DispatcherError("BACKUP_SCOPE")
    if not destination.parent.is_dir():
        raise DispatcherError("BACKUP_IO")
    partial = destination.with_name(".poster-backup-" + uuid.uuid4().hex + ".partial")
    try:
        with offline(roots.values()):
            plan = inventory(roots)
            if not plan["scopes"]:
                raise DispatcherError("BACKUP_SCOPE")
            space(destination.parent, plan["bytes"] * 3 + MAX_MANIFEST * 2)
            with tempfile.TemporaryDirectory(prefix=".poster-backup-", dir=destination.parent) as temp:
                temp = private_dir(temp)
                archive = temp / "snapshot.zip"
                manifest = dict(
                    format=FORMAT,
                    binding=binding(),
                    created=time.time(),
                    scopes=plan["scopes"],
                    directories=plan["directories"],
                    files=[],
                    excluded=[
                        "browser caches",
                        "temporary files",
                        "IPC/lock files",
                        "unselected external folders",
                    ],
                )
                from poster.dispatcher.launcher import build_info

                manifest["build"] = build_info()["commit"]
                with zipfile.ZipFile(archive, "x", compression=zipfile.ZIP_STORED, allowZip64=True) as output:
                    restrict(archive)
                    for i, item in enumerate(plan["files"]):
                        source = item["source"]
                        before = source.stat()
                        actual = source
                        with source.open("rb") as f:
                            is_sqlite = f.read(16) == b"SQLite format 3\x00"
                        if is_sqlite:
                            actual = temp / "database.sqlite"
                            sqlite_snapshot(source, actual)
                        member = "files/" + str(i)
                        sha = hashlib.sha256()
                        count = 0
                        with actual.open("rb") as src, output.open(member, "w", force_zip64=True) as dst:
                            while raw := src.read(crypto.CHUNK):
                                count += len(raw)
                                if count > MAX_FILE:
                                    raise DispatcherError("BACKUP_LIMIT")
                                dst.write(raw)
                                sha.update(raw)
                        after = source.stat()
                        if (before.st_ino, before.st_size, before.st_mtime_ns) != (
                            after.st_ino,
                            after.st_size,
                            after.st_mtime_ns,
                        ):
                            raise DispatcherError("BACKUP_IO")
                        if is_sqlite:
                            actual.unlink()
                        manifest["files"].append(
                            dict(
                                scope=item["scope"],
                                path=item["path"],
                                size=count,
                                sha256=sha.hexdigest(),
                                member=member,
                            )
                        )
                    encoded = json.dumps(manifest, ensure_ascii=False, allow_nan=False).encode()
                    if len(encoded) > MAX_MANIFEST:
                        raise DispatcherError("BACKUP_LIMIT")
                    output.writestr("manifest.json", encoded)
                crypto.encrypt(archive, partial, password)
                checksum = digest(partial)
                os.link(partial, destination)
                restrict(destination)
                return dict(
                    status="PASSED",
                    file_count=len(manifest["files"]),
                    bytes=sum(f["size"] for f in manifest["files"]),
                    sha256=checksum,
                    scopes=[s["name"] for s in manifest["scopes"]],
                    build=manifest["build"],
                )
    except DispatcherError:
        raise
    except Exception as exc:
        raise DispatcherError("BACKUP_IO") from exc
    finally:
        partial.unlink(missing_ok=True)


def relative(value):
    if not isinstance(value, str) or not value or len(value) > 1000 or "\\" in value or "\x00" in value:
        raise DispatcherError("BACKUP_INVALID")
    p = PurePosixPath(value)
    if (
        p.is_absolute()
        or p.as_posix() != value
        or any(part in {"", ".", ".."} or ":" in part for part in p.parts)
        or (
            os.name == "nt"
            and any(
                part.rstrip(". ") != part
                or any(c in part for c in '<>"|?*')
                or part.split(".")[0].upper()
                in {
                    "CON",
                    "PRN",
                    "AUX",
                    "NUL",
                    *[f"COM{i}" for i in range(1, 10)],
                    *[f"LPT{i}" for i in range(1, 10)],
                }
                for part in p.parts
            )
        )
    ):
        raise DispatcherError("BACKUP_INVALID")
    return p


def validate(archive, roots):
    infos = archive.infolist()
    if len(infos) > MAX_FILES + 1 or len({i.filename for i in infos}) != len(infos):
        raise DispatcherError("BACKUP_INVALID")
    if any(
        i.compress_type != zipfile.ZIP_STORED
        or i.flag_bits & 1
        or (i.external_attr >> 16) & 0o170000 not in {0, 0o100000}
        for i in infos
    ):
        raise DispatcherError("BACKUP_INVALID")
    info = archive.getinfo("manifest.json")
    if info.file_size > MAX_MANIFEST:
        raise DispatcherError("BACKUP_INVALID")
    manifest = json.loads(archive.read("manifest.json"))
    if manifest["format"] != FORMAT or manifest["binding"] != binding():
        raise DispatcherError("BACKUP_SCOPE")
    scopes = manifest["scopes"]
    files = manifest["files"]
    dirs = manifest["directories"]
    if (
        not isinstance(scopes, list)
        or not isinstance(files, list)
        or not isinstance(dirs, list)
        or not 1 <= len(scopes) <= len(roots)
        or len(files) + len(dirs) > MAX_FILES
    ):
        raise DispatcherError("BACKUP_INVALID")
    by_scope = {s["name"]: s for s in scopes}
    if len(by_scope) != len(scopes):
        raise DispatcherError("BACKUP_INVALID")
    for scope in scopes:
        if (
            scope["name"] not in roots
            or scope["path"] != str(roots[scope["name"]])
            or scope["kind"] not in {"file", "directory"}
        ):
            raise DispatcherError("BACKUP_SCOPE")
    seen = set()
    file_paths = set()
    for item in dirs:
        if "member" in item or by_scope[item["scope"]]["kind"] != "directory":
            raise DispatcherError("BACKUP_INVALID")
    for item in files:
        if "member" not in item:
            raise DispatcherError("BACKUP_INVALID")
        file_paths.add((item["scope"], os.path.normcase(item["path"])))
    for scope in scopes:
        if scope["kind"] == "file" and (scope["name"], "") not in file_paths:
            raise DispatcherError("BACKUP_INVALID")
    total = 0
    members = {"manifest.json"}
    for item in [*dirs, *files]:
        scope = by_scope[item["scope"]]
        rel = item["path"]
        pair = (item["scope"], os.path.normcase(rel))
        if pair in seen or (scope["kind"] == "file" and rel != ""):
            raise DispatcherError("BACKUP_INVALID")
        seen.add(pair)
        if scope["kind"] == "directory":
            relative(rel)
            if any(
                (item["scope"], os.path.normcase(p.as_posix())) in file_paths
                for p in PurePosixPath(rel).parents
                if p.as_posix() != "."
            ):
                raise DispatcherError("BACKUP_INVALID")
        if "member" not in item:
            continue
        member = item["member"]
        if not member.startswith("files/") or not member[6:].isdigit() or member in members:
            raise DispatcherError("BACKUP_INVALID")
        members.add(member)
        size = item["size"]
        total += size
        if (
            type(size) is not int
            or not 0 <= size <= MAX_FILE
            or total > crypto.MAX_BYTES
            or archive.getinfo(member).file_size != size
        ):
            raise DispatcherError("BACKUP_INVALID")
        sha = hashlib.sha256()
        with archive.open(member) as src:
            while raw := src.read(crypto.CHUNK):
                sha.update(raw)
        if sha.hexdigest() != item["sha256"]:
            raise DispatcherError("BACKUP_INVALID")
    if {i.filename for i in infos} != members:
        raise DispatcherError("BACKUP_INVALID")
    return manifest


@contextmanager
def opened(source, password, roots, temporary_parent):
    roots = roots_checked(roots)
    try:
        source = plain_path(source)
        space(temporary_parent, source.stat().st_size * 2 + MAX_MANIFEST)
        with tempfile.TemporaryDirectory(prefix=".poster-verify-", dir=temporary_parent) as temp:
            temp = private_dir(temp)
            decrypted = temp / "snapshot.zip"
            crypto.decrypt(source, decrypted, password)
            with zipfile.ZipFile(decrypted, "r") as archive:
                manifest = validate(archive, roots)
                yield archive, manifest
    except DispatcherError:
        raise
    except Exception as exc:
        raise DispatcherError("BACKUP_INVALID") from exc


def inspect(source, password, roots):
    source = plain_path(source)
    with opened(source, password, roots, source.parent) as (_, manifest):
        return manifest
