"""Host-owned execution limits and portable file capabilities; no GUI imports."""

from dataclasses import dataclass, field
import os
from pathlib import Path, PureWindowsPath
import stat
import tempfile
import time

from .model import GraphError

MAX_DATA = 1_000_000
MAX_FILE = 10_000_000


def lifecycle_path(path):
    return any(
        part == ".poster-product-locks"
        or part.startswith((".poster-stage-", ".poster-verify-", ".poster-backup-", "poster-product-start-"))
        or ".poster-before-" in part
        or ".poster-interrupted-" in part
        for part in path.parts
    )


@dataclass
class Budget:
    steps: int
    seconds: float = 300
    started: float = field(default_factory=time.monotonic)
    used: int = 0
    result_bytes: int = 0
    stopped: bool = False
    file_bytes: int = 0

    def remaining(self):
        return self.seconds - (time.monotonic() - self.started)

    def consume(self):
        if self.stopped:
            raise GraphError("RUN_STOPPED")
        if self.remaining() <= 0:
            raise GraphError("RUN_TIMEOUT")
        if self.used >= self.steps:
            raise GraphError("STEP_LIMIT")
        self.used += 1


class FileSandbox:
    def __init__(self, root=None, *, allow_external=False):
        from poster.product.locking import registry_path

        self.root = Path(root or tempfile.mkdtemp(prefix="poster-run-")).absolute().resolve()
        if lifecycle_path(self.root) or self.root.is_relative_to(registry_path()):
            raise GraphError("FILE_PATH_INVALID")
        self.root.mkdir(parents=True, exist_ok=True)
        self.allow_external = allow_external
        self.budget = None

    def path(self, text, *, allow_root=False):
        if not text or "\x00" in str(text):
            raise GraphError("FILE_PATH_INVALID")
        text = str(text)
        if lifecycle_path(Path(text.replace("\\", "/"))):
            raise GraphError("FILE_PATH_INVALID")
        if os.name != "nt" and PureWindowsPath(text).drive:
            raise GraphError("FILE_OUTSIDE_WORKDIR")
        path = Path(text.replace("\\", "/"))
        path = path if path.is_absolute() else self.root / path
        resolved = path.resolve()
        from poster.product.locking import registry_path

        if lifecycle_path(resolved) or resolved.is_relative_to(registry_path()):
            raise GraphError("FILE_PATH_INVALID")
        if not self.allow_external and not resolved.is_relative_to(self.root):
            raise GraphError("FILE_OUTSIDE_WORKDIR")
        for part in [path, *path.parents]:
            try:
                info = part.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise GraphError("FILE_LINK_FORBIDDEN")
        if resolved == self.root and not allow_root:
            raise GraphError("FILE_PATH_INVALID")
        return resolved

    def read(self, text, limit=MAX_FILE):
        path = self.path(text)
        if not path.is_file():
            raise GraphError("FILE_MISSING")
        with path.open("rb") as stream:
            data = stream.read(limit + 1)
        if len(data) > limit:
            raise GraphError("FILE_LIMIT")
        return data

    def write(self, text, raw, *, overwrite=False):
        from .storage import identity

        if len(raw) > MAX_FILE:
            raise GraphError("FILE_LIMIT")
        if self.budget:
            self.budget.file_bytes += len(raw)
            if self.budget.file_bytes > 100_000_000:
                raise GraphError("FILE_LIMIT")
        path = self.path(text)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path(text)  # Recheck after creating parents.
        if path.exists() and not overwrite:
            raise GraphError("FILE_EXISTS")
        temporary = path.with_name(".poster-" + identity() + ".tmp")
        try:
            with temporary.open("xb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            if overwrite:
                os.replace(temporary, path)
            else:
                # Exclusive publication: concurrent runs never silently overwrite.
                os.link(temporary, path)
        except FileExistsError as exc:
            raise GraphError("FILE_EXISTS") from exc
        finally:
            temporary.unlink(missing_ok=True)
        return str(path.relative_to(self.root)) if path.is_relative_to(self.root) else str(path)

    def copy(self, source, destination, *, overwrite=False):
        from poster.portability.blobs import FileBlob, copy_blob, MAX_BUNDLE_BYTES
        from poster.cloud.errors import CloudError

        try:
            blob = FileBlob.capture(self.path(source))
            if self.budget:
                self.budget.file_bytes += len(blob)
                if self.budget.file_bytes > MAX_BUNDLE_BYTES:
                    raise GraphError("FILE_LIMIT")
                if self.budget.stopped or self.budget.remaining() <= 0:
                    raise GraphError("RUN_TIMEOUT")
            path = self.path(destination)
            copy_blob(blob, path, overwrite=overwrite)
            return str(path.relative_to(self.root)) if path.is_relative_to(self.root) else str(path)
        except FileExistsError as exc:
            raise GraphError("FILE_EXISTS") from exc
        except CloudError as exc:
            raise GraphError("FILE_LIMIT" if exc.code == "PACKAGE_LIMIT" else exc.code) from exc
