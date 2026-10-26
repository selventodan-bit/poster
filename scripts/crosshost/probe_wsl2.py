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
        subprocess.run(["wsl.exe", "--shutdown"], capture_output=True, check=False)
        code = """import json,os,platform
from pathlib import Path
mounts=Path('/proc/mounts').read_text()
interop=Path('/proc/sys/fs/binfmt_misc/WSLInterop')
print(json.dumps(dict(os=platform.system(),kernel=platform.release(),arch=platform.machine(),
 pid=os.getpid(),boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
 mount_types=sorted(set(line.split()[2] for line in mounts.splitlines())),
 windows_mountpoints=[line.split()[1] for line in mounts.splitlines() if 'drvfs' in line],
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
        value["linux"] = guest
        if guest["os"] != "Linux" or guest["arch"] != "x86_64":
            raise ValueError("LINUX_X86_64_REQUIRED")
        if "WSL2" not in guest["kernel"].upper():
            raise ValueError("REAL_WSL2_KERNEL_REQUIRED")
        if guest["windows_drives_mounted"]:
            raise ValueError("WINDOWS_DRIVES_MOUNTED")
        if guest["interop_enabled"]:
            raise ValueError("WINDOWS_INTEROP_ENABLED")
        value.update(status="WSL2_READY", linux=guest)
    except Exception as exc:
        value.update(status="UNAVAILABLE", error_type=type(exc).__name__)
        if isinstance(exc, ValueError) and str(exc).isupper():
            value["reason"] = str(exc)
        raise
    finally:
        target.write_text(json.dumps(value, indent=2), encoding="utf-8")
        print("::notice title=Cross-host infrastructure capability::" + json.dumps(value))


if __name__ == "__main__":
    main()
