"""Separate real frozen Task Scheduler registration/start/removal, always cleaned up."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import time


def check(bundle, output):
    import psutil
    from poster.dispatcher.host import request
    from poster.dispatcher.processes import terminate_tree

    output.mkdir(parents=True, exist_ok=False)
    root = output / "Temporary logon task Ж"
    service = bundle / "PosterService.exe"
    windows = Path(os.environ["SystemRoot"])
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in {"POSTER_SESSION_KEY", "PYTHONHOME", "PYTHONPATH", "POSTER_PYTHON"}
    }
    env["PATH"] = os.pathsep.join(map(str, [windows / "System32", windows]))
    evidence, info, registered = dict(status="FAILED"), None, False

    def configure(action):
        result = subprocess.run(
            [str(service), "--workspace", str(root), "--autostart", action],
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=20,
        )
        assert result.returncode == 0, "Frozen autostart command failed: " + action
        return json.loads(result.stdout)

    try:
        # A unique workspace produces a unique task, never touching a user's other task.
        assert not configure("status")["exists"]
        registered = True  # Cleanup even if registration succeeds but its response is lost.
        value = configure("enable")
        assert value["enabled"] and value["matches"] and value["exists"]
        assert configure("status") == value
        subprocess.run(
            [str(windows / "System32/schtasks.exe"), "/Run", "/TN", value["task"]],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
        )
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            try:
                info = request(root, "ping")
                break
            except ValueError:
                time.sleep(0.1)
        assert info and Path(psutil.Process(info["pid"]).exe()).resolve() == service.resolve()
        assert info["commit"] == os.environ["EXPECTED_POSTER_COMMIT"]
        # Duplicate logon/start is harmless under the existing host singleton lock.
        duplicate = subprocess.run([str(service), "--workspace", str(root)], env=env, timeout=15)
        assert duplicate.returncode == 1 and request(root, "ping")["pid"] == info["pid"]
        evidence = dict(
            status="PASSED",
            task=value["task"],
            enabled=True,
            matches=True,
            frozen_service=True,
            singleton=True,
            commit=info["commit"],
        )
    finally:
        try:
            if registered:
                configure("disable")
                assert not configure("status")["exists"]
                evidence["removed"] = True
        finally:
            if info:
                try:
                    request(root, "shutdown")
                    psutil.Process(info["pid"]).wait(20)
                finally:
                    terminate_tree(info["pid"], info["birth"])
            (output / "report.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    check(args.bundle.resolve(), args.output.resolve())
