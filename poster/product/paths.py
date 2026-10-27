"""Stable per-user Maker data; explicit adoption preserves legacy keys in place."""

import json
import os
from pathlib import Path
import sys

from poster.dispatcher.security import plain_path, private_dir, safe_json
from poster.dispatcher.models import DispatcherError


def product_home():
    base = (
        Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
        if os.name == "nt"
        else Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
    )
    return plain_path(base / "Poster")


def meaningful(root):
    return any(
        (root / name).exists()
        for name in ("maker-projects", "maker-qt-layout.json", "private", "state.sqlite", "projects")
    )


def checked_workspace(root):
    from .backup import roots_checked

    try:
        return roots_checked({"maker": root})["maker"]
    except DispatcherError as exc:
        raise DispatcherError("WORKSPACE_INVALID") from exc


def maker_root():
    from .locking import connection_lease

    if os.environ.get("POSTER_MAKER_WORKSPACE"):
        return checked_workspace(os.environ["POSTER_MAKER_WORKSPACE"])
    locator = product_home() / "maker-location.json"
    with connection_lease(locator):
        if locator.exists():
            try:
                if locator.stat().st_size > 20000:
                    raise ValueError()
                value = json.loads(locator.read_bytes())
                if value["format"] != "poster.maker.location.v1":
                    raise ValueError()
                selected = plain_path(value["path"])
                if selected == Path(selected.anchor) or Path.home().resolve().is_relative_to(selected):
                    raise ValueError()
                return checked_workspace(selected)
            except DispatcherError:
                raise
            except Exception as exc:
                raise DispatcherError("WORKSPACE_INVALID") from exc
    legacy = plain_path(Path.cwd() / ".poster")
    if not getattr(sys, "frozen", False):
        return checked_workspace(legacy)
    if meaningful(legacy):
        # Keep encrypted profiles and relative project references in their old
        # location. Remember it before the executable moves on a later update.
        select_workspace(legacy)
        return legacy
    return checked_workspace(product_home() / "Maker")


def select_workspace(root):
    from .locking import connection_lease

    root = checked_workspace(root)
    if root == Path(root.anchor) or root == Path.home().resolve() or not meaningful(root):
        raise DispatcherError("WORKSPACE_INVALID")
    locator = product_home() / "maker-location.json"
    with connection_lease(root), connection_lease(locator):
        safe_json(
            private_dir(product_home()) / "maker-location.json",
            dict(format="poster.maker.location.v1", path=str(root)),
        )
    return root
