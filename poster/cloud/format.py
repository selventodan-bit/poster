"""One bounded portable data contract for Drive, Windows, Linux and notebooks."""

import hashlib
import json
from pathlib import PurePosixPath, PureWindowsPath
import re

from .errors import CloudError

MAX_FILES = 128
MAX_BYTES = 20_000_000
MAX_METADATA = 200_000


def canonical(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def digest(raw):
    from poster.portability.blobs import FileBlob

    if isinstance(raw, FileBlob):
        return raw.verify().sha256
    return hashlib.sha256(raw).hexdigest()


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", value):
        raise CloudError("INTEGRITY")
    return value


def version_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
        raise CloudError("INTEGRITY")
    return value


def relative(value):
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 400
        or any(ord(c) < 32 or c in '\\:*?"<>|' for c in value)
        or PureWindowsPath(value).drive
    ):
        raise CloudError("PATH_FORBIDDEN")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or str(path) != value
        or any(
            part in {".", ".."}
            or part.endswith((".", " "))
            or re.fullmatch(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", part)
            for part in path.parts
        )
    ):
        raise CloudError("PATH_FORBIDDEN")
    return value


def metadata(raw):
    try:
        if len(raw) > MAX_METADATA:
            raise ValueError()
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError()
        canonical(value)
        return value
    except (ValueError, TypeError, RecursionError) as exc:
        raise CloudError("INTEGRITY") from exc
