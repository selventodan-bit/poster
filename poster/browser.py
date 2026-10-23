"""Discover existing Chromium browsers. Downloads are a separate, explicit action."""

import asyncio
import os
from pathlib import Path
import shutil
import subprocess
import sys


def browser_candidates():
    candidates = []
    if sys.platform == "win32":
        import winreg

        for name in ("chrome.exe", "chromium.exe", "msedge.exe"):
            for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
                for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
                    try:
                        with winreg.OpenKey(
                            hive,
                            "SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\App Paths\\" + name,
                            0,
                            winreg.KEY_READ | view,
                        ) as key:
                            candidates.append(winreg.QueryValue(key, None).strip('"'))
                    except OSError:
                        pass
        for variable in ("LOCALAPPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)"):
            base = os.environ.get(variable)
            if base:
                for suffix in (
                    "Google/Chrome/Application/chrome.exe",
                    "Chromium/Application/chrome.exe",
                    "Microsoft/Edge/Application/msedge.exe",
                ):
                    candidates.append(str(Path(base) / suffix))
    elif sys.platform == "darwin":
        candidates.extend(
            [
                "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                "/Applications/Chromium.app/Contents/MacOS/Chromium",
            ]
        )
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "msedge"):
        value = shutil.which(name)
        if value:
            candidates.append(value)
    return candidates


def find_browser(managed_executable=None):
    explicit = os.environ.get("POSTER_CHROMIUM")
    if explicit:
        if not Path(explicit).is_file():
            raise ValueError("BROWSER_PATH_INVALID")
        return str(Path(explicit).resolve())
    # Installed Chrome/Chromium/Edge first, already downloaded Playwright second.
    for candidate in [*browser_candidates(), managed_executable]:
        if candidate and Path(candidate).is_file():
            return str(Path(candidate).resolve())
    caches = [Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", ".poster/browsers"))]
    if os.environ.get("LOCALAPPDATA"):
        caches.append(Path(os.environ["LOCALAPPDATA"]) / "ms-playwright")
    caches.append(Path.home() / ".cache/ms-playwright")
    for cache in caches:
        for pattern in (
            "chromium-*/chrome-win64/chrome.exe",
            "chromium-*/chrome-win/chrome.exe",
            "chromium-*/chrome-linux/chrome",
            "chromium-*/chrome-linux64/chrome",
        ):
            for candidate in sorted(cache.glob(pattern), reverse=True):
                if candidate.is_file():
                    return str(candidate.resolve())
    return None


async def install_browser():
    """Caller must have obtained explicit consent. Works inside a frozen executable too."""
    from playwright._impl._driver import compute_driver_executable, get_driver_env

    node, cli = compute_driver_executable()
    process = await asyncio.create_subprocess_exec(
        str(node),
        str(cli),
        "install",
        "chromium",
        env=get_driver_env(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    try:
        if await process.wait():
            raise ValueError("BROWSER_INSTALL_FAILED")
    finally:
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 5)
            except TimeoutError:
                process.kill()
                await process.wait()
