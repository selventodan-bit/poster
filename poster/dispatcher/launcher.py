"""Explicit source/frozen roles. Never look for Python in a frozen installation."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

from .models import DispatcherError


def build_info():
    if not getattr(sys, "frozen", False):
        return {"commit": "source", "modes": ["service", "worker", "gui"]}
    try:
        value = json.loads((Path(sys.executable).parent / "BUILD_INFO.json").read_bytes())
        if value["commit"] != os.environ.get("POSTER_FROZEN_COMMIT"):
            raise ValueError()
        return value
    except Exception as exc:
        raise DispatcherError("BUILD_INVALID") from exc


def command(role, *args):
    if role not in {"worker", "service", "gui"}:
        raise DispatcherError("PARAMETERS_INVALID")
    if not getattr(sys, "frozen", False):
        module = {"worker": "worker", "service": "host", "gui": "qt_app"}[role]
        return [sys.executable, "-m", "poster.dispatcher." + module, *map(str, args)]
    info = build_info()
    try:
        record = info["launchers"][role]
        path = Path(sys.executable).parent / record["file"]
        if path.parent != Path(sys.executable).parent or path.is_symlink():
            raise ValueError()
        if hashlib.sha256(path.read_bytes()).hexdigest() != record["sha256"]:
            raise ValueError()
        return [str(path), *map(str, args)]
    except Exception as exc:
        raise DispatcherError("WORKER_UNAVAILABLE") from exc


def environment():
    result = {
        k: v
        for k, v in os.environ.items()
        if k
        not in {
            "POSTER_SESSION_KEY",
            "PYTHONHOME",
            "PYTHONPATH",
            "POSTER_PYTHON",
            "POSTER_DISPATCHER_RUN",
            "POSTER_WORKER_PID_PIPE",
        }
        and not k.startswith("_PYI_")
    }
    result["PYTHONUTF8"] = "1"
    return result


def process_options():
    return (
        {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}
        if os.name == "nt"
        else {"start_new_session": True}
    )
