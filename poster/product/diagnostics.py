"""An allowlist report: no paths, URLs, arguments, log bodies or configuration values."""

from contextlib import closing
from importlib.metadata import PackageNotFoundError, version
import json
import os
from pathlib import Path
import platform
import re
import shutil
import sqlite3
import sys

from poster.dispatcher.security import plain_path
from .locking import connection_lease


def build_info():
    directory = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path.cwd()
    try:
        path = directory / "BUILD_INFO.json"
        if path.stat().st_size > 100000:
            return {}
        value = json.loads(path.read_bytes())
        rules = {
            "commit": r"[a-f0-9]{40}",
            "version": r"[0-9]+[.][0-9]+(?:[.][0-9]+)?(?:rc[0-9]+)?",
            "ui": r"qt|tk",
            "package": r"poster-(?:maker-windows|windows|linux)",
        }
        return {
            key: str(value[key])
            for key, pattern in rules.items()
            if key in value and re.fullmatch(pattern, str(value[key]))
        }
    except (OSError, ValueError, TypeError):
        return {}


def collect(roots):
    from poster.browser import find_browser

    report = dict(
        format="poster.diagnostics.v1",
        os=platform.system(),
        architecture=platform.machine(),
        python=platform.python_version(),
        frozen=bool(getattr(sys, "frozen", False)),
        build=build_info(),
        packages={},
        storage={},
    )
    for package in ("poster-http", "PySide6", "playwright", "cryptography", "httpx", "psutil"):
        try:
            report["packages"][package] = version(package)
        except PackageNotFoundError:
            report["packages"][package] = "UNAVAILABLE"
    try:
        report["browser"] = "AVAILABLE" if find_browser() else "MISSING"
    except Exception:
        report["browser"] = "INVALID_CONFIGURATION"
    for name, value in roots.items():
        # Scope identifiers are fixed by the caller; user-defined names never
        # become keys in a report intended for sharing.
        if name not in {"maker", "dispatcher", "cloud", "location", "legacy_key"}:
            continue
        try:
            path = plain_path(value)
            parent = path
            while not parent.exists():
                parent = parent.parent
            report["storage"][name] = dict(
                exists=path.exists(),
                writable=os.access(parent, os.W_OK),
                free_bytes=shutil.disk_usage(parent).free,
            )
        except Exception:
            report["storage"][name] = dict(code="UNAVAILABLE")
    path = plain_path(roots["dispatcher"]) if "dispatcher" in roots else None
    if path is not None and (path / "dispatcher.sqlite").is_file():
        try:
            with (
                connection_lease(path),
                closing(
                    sqlite3.connect((path / "dispatcher.sqlite").as_uri() + "?mode=ro", uri=True, timeout=2)
                ) as db,
            ):
                report["queue"] = {
                    status: count
                    for status, count in db.execute("SELECT status,count(*) FROM jobs GROUP BY status")
                    if status
                    in {
                        "PENDING",
                        "LEASED",
                        "RUNNING",
                        "SUCCEEDED",
                        "FAILED",
                        "CANCELLED",
                        "UNKNOWN",
                        "REVIEW_REQUIRED",
                        "TIMED_OUT",
                    }
                }
                report["scheduler"] = dict(
                    enabled=db.execute("SELECT count(*) FROM schedules WHERE enabled=1").fetchone()[0]
                )
                report["agents"] = {
                    state: count
                    for state, count in db.execute("SELECT state,count(*) FROM remote_agents GROUP BY state")
                    if state in {"APPROVED", "PENDING", "REVOKED"}
                }
        except Exception:
            report["queue"] = dict(code="UNAVAILABLE")
    report["external_validation"] = dict(
        google_drive="CLOUD_PROVIDER_NOT_EXECUTED",
        colab="CLOUD_PROVIDER_NOT_EXECUTED",
        molab="CLOUD_PROVIDER_NOT_EXECUTED",
        public_wan="EXTERNAL_VALIDATION_REQUIRED",
    )
    return report
