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


@dataclass
class Budget:
    steps: int
    seconds: float = 300
    started: float = field(default_factory=time.monotonic)
    used: int = 0
    result_bytes: int = 0
    stopped: bool = False

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
        self.root = Path(root or tempfile.mkdtemp(prefix="poster-run-")).absolute().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.allow_external = allow_external

    def path(self, text):
        if not text or "\x00" in str(text):
            raise GraphError("FILE_PATH_INVALID")
        text = str(text)
        if os.name != "nt" and PureWindowsPath(text).drive:
            raise GraphError("FILE_OUTSIDE_WORKDIR")
        path = Path(text.replace("\\", "/"))
        path = path if path.is_absolute() else self.root / path
        resolved = path.resolve()
        if not self.allow_external and not resolved.is_relative_to(self.root):
            raise GraphError("FILE_OUTSIDE_WORKDIR")
        for part in [path, *path.parents]:
            try:
                info = part.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise GraphError("FILE_LINK_FORBIDDEN")
        if resolved == self.root:
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
