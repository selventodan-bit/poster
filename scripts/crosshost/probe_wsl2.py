"""Prove real separate Linux kernel in a standard hosted Windows runner.

Capability evidence only: never an acceptance PASSED or a Linux/Linux substitute.
"""

import json
import os
from pathlib import Path
import platform
import subprocess


def main():
    if os.name != "nt":
        raise RuntimeError("Requires the real hosted Windows runner")
    target = Path(".poster/crosshost-infrastructure/probe.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    value = dict(
        status="NOT_EXECUTED",
        full_cross_host_acceptance=False,
        topology="Windows hosted runner + separate WSL2 Linux VM",
        windows=dict(
            os=platform.system(), release=platform.release(), version=platform.version(), pid=os.getpid()
        ),
        linux=None,
    )
    try:
        subprocess.run(["wsl.exe", "--terminate", "Ubuntu-24.04"], capture_output=True, check=False)
        code = """import json,os,platform
from pathlib import Path
mounts=Path('/proc/mounts').read_text()
interop=Path('/proc/sys/fs/binfmt_misc/WSLInterop')
print(json.dumps(dict(os=platform.system(),kernel=platform.release(),arch=platform.machine(),
 pid=os.getpid(),boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
 windows_drives_mounted=any('drvfs' in line for line in mounts.splitlines()),
 interop_enabled=interop.exists() and 'enabled' in interop.read_text())))
"""
        result = subprocess.run(
            [
                "wsl.exe",
                "--distribution",
                "Ubuntu-24.04",
                "--user",
                "root",
                "--exec",
                "/usr/bin/python3",
                "-c",
                code,
            ],
            capture_output=True,
            timeout=180,
        )
        if result.returncode:
            raise RuntimeError("WSL2 boot failed")
        guest = json.loads(result.stdout.decode("utf-8"))
        assert guest["os"] == "Linux" and guest["arch"] == "x86_64"
        assert "WSL2" in guest["kernel"].upper(), "A real WSL2 kernel is required"
        assert not guest["windows_drives_mounted"] and not guest["interop_enabled"]
        value.update(status="WSL2_READY", linux=guest)
    except Exception as exc:
        value.update(status="UNAVAILABLE", error_type=type(exc).__name__)
        raise
    finally:
        target.write_text(json.dumps(value, indent=2), encoding="utf-8")
        print("::notice title=Cross-host infrastructure capability::" + json.dumps(value))


if __name__ == "__main__":
    main()
