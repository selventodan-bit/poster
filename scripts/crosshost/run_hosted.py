"""Actual frozen Windows / isolated WSL2 Linux E2E on a standard public runner.

No service secrets in argv, logs or artifacts. No drive sharing or interop.
The Linux VM is a separate kernel/OS; this is not a public WAN deployment test.
"""

import hashlib
import ipaddress
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tarfile
import zipfile

COMMIT = "f411f806d5d0fa498b556bd4e4d2c6a955bcd992"
WINDOWS_SHA = "4fabdfd5581133624cdb26e70ae6aeaa9563311bc1754dc825a7736b1af02e54"
LINUX_SHA = "bcafbd27f20d8f7f8ddb32222b68efada9870d9c2a67e92c4fe473055859e731"
DISTRO = "Ubuntu-24.04"
HOST = "poster-linux.test"
GUEST_HOME = "/home/postercheck"
PYTHON = GUEST_HOME + "/poster-program/current/bin/python"

BOOTSTRAP = r"""
import ipaddress,json,os,subprocess,sys
from pathlib import Path
phase='bootstrap'
try:
 data=json.load(sys.stdin)
 def run(args):
  subprocess.run(args,check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
   env={**os.environ,'DEBIAN_FRONTEND':'noninteractive'},timeout=600)
 phase='os_packages'
 run(['apt-get','update'])
 run(['apt-get','install','-y','openssh-server','python3.12-venv','ca-certificates'])
 phase='unprivileged_account'
 run(['useradd','--create-home','--shell','/bin/bash','postercheck'])
 home=Path('/home/postercheck')
 import pwd
 account=pwd.getpwnam('postercheck')
 ssh=home/'.ssh'; ssh.mkdir(mode=0o700)
 auth=ssh/'authorized_keys'; auth.write_text(data['public_key']+'\n'); auth.chmod(0o600)
 for path in [ssh,auth]: os.chown(path,account.pw_uid,account.pw_gid)
 # A valid but unusable password hash unlocks public-key authentication only.
 run(['usermod','--password','*NP*','postercheck'])
 phase='pinned_ssh_server'
 run(['ssh-keygen','-A'])
 conf=Path('/etc/ssh/sshd_config.d/00-postercheck.conf')
 conf.write_text('PasswordAuthentication no\nKbdInteractiveAuthentication no\nPermitRootLogin no\nAllowUsers postercheck\nPubkeyAuthentication yes\n')
 run(['systemctl','restart','ssh'])
 raw=subprocess.check_output(['ip','-j','-4','addr','show','dev','eth0'])
 addresses=json.loads(raw)[0]['addr_info']
 ip=next(a['local'] for a in addresses if a.get('scope')=='global')
 ipaddress.IPv4Address(ip)
 with Path('/etc/hosts').open('a') as stream: stream.write('\n'+ip+' poster-linux.test\n')
 key=Path('/etc/ssh/ssh_host_ed25519_key.pub').read_text().split()
 print(json.dumps(dict(ip=ip,host_key=' '.join(key[:2]),uid=account.pw_uid)))
except Exception as exc:
 print(json.dumps(dict(failed_phase=phase,error_type=type(exc).__name__)))
 sys.exit(1)
"""

INSTALL = r"""
import hashlib,json,subprocess,sys,tarfile
from pathlib import Path
home=Path('/home/postercheck')
archive=home/'poster-linux.tar.gz'
assert hashlib.sha256(archive.read_bytes()).hexdigest() == 'bcafbd27f20d8f7f8ddb32222b68efada9870d9c2a67e92c4fe473055859e731'
release=home/'release'; release.mkdir(mode=0o700)
with tarfile.open(archive) as stream: stream.extractall(release,filter='data')
release=release/'poster-linux'
assert json.loads((release/'release.json').read_bytes())['commit'] == 'f411f806d5d0fa498b556bd4e4d2c6a955bcd992'
subprocess.run([sys.executable,str(release/'install_remote.py'),'install','--release',str(release),
 '--role','agent','--program',str(home/'poster-program'),'--state',str(home/'install-placeholder'),
 '--unit-dir',str(home/'units-staging'),'--offline'],check=True)
"""


def execute(args, *, data=None, timeout=600, env=None):
    result = subprocess.run(args, input=data, capture_output=True, timeout=timeout, env=env)
    if result.returncode:
        # Never propagate private stdout/stderr or command text to Actions logs.
        raise RuntimeError("CHILD_PROCESS_FAILED")
    return result.stdout


def wsl(args, **kwargs):
    environment = {
        k: v
        for k, v in os.environ.items()
        if k not in {"GH_TOKEN", "GITHUB_TOKEN", "POSTER_SESSION_KEY", "PYTHONPATH", "PYTHONHOME"}
    }
    environment["WSLENV"] = ""
    return execute(
        ["wsl.exe", "--distribution", DISTRO, "--user", "root", "--exec", *args],
        env=environment,
        **kwargs,
    )


def main():
    global COMMIT, WINDOWS_SHA, LINUX_SHA, INSTALL
    if os.name != "nt" or os.environ.get("GITHUB_ACTIONS") != "true":
        raise RuntimeError("REQUIRES_STANDARD_HOSTED_WINDOWS")
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from poster.dispatcher.security import private_dir, safe_json, restrict

    root = Path(__file__).resolve().parents[2]
    if os.environ.get("POSTER_CROSSHOST_CURRENT") == "1":
        import re

        commit = execute(["git", "rev-parse", "HEAD"]).decode().strip()
        if not re.fullmatch("[a-f0-9]{40}", commit) or commit != os.environ.get("GITHUB_SHA"):
            raise RuntimeError("CURRENT_COMMIT_MISMATCH")
        checksums = []
        for role, filename in [("windows", "poster-maker-windows.sha256"), ("linux", "poster-linux.sha256")]:
            expected = (root / ".poster" / ("original-" + role) / filename).read_text().split()[0]
            if not re.fullmatch("[a-f0-9]{64}", expected):
                raise RuntimeError("CURRENT_CHECKSUM_INVALID")
            checksums.append(expected)
        INSTALL = INSTALL.replace(COMMIT, commit).replace(LINUX_SHA, checksums[1])
        COMMIT, WINDOWS_SHA, LINUX_SHA = commit, *checksums
    public = root / ".poster/crosshost-public"
    public.mkdir(parents=True, exist_ok=True)
    private = private_dir(root / ".poster/crosshost-private")
    proof = dict(
        status="NOT_EXECUTED",
        full_cross_host_acceptance=False,
        topology="Windows hosted runner + separate WSL2 Linux VM (private TCP)",
        production_commit=COMMIT,
        harness_commit=execute(["git", "rev-parse", "HEAD"]).decode().strip(),
        artifacts={},
    )
    execute(
        [
            sys.executable,
            str(root / "scripts/check_remote_crosshost.py"),
            "--not-executed",
            "--output",
            str(public),
        ]
    )
    keeper, phase, hosts_line = None, "verified packages", None
    hosts = Path(os.environ["SystemRoot"]) / "System32/drivers/etc/hosts"
    try:
        for role, filename, expected in [
            ("windows", "poster-maker-windows.zip", WINDOWS_SHA),
            ("linux", "poster-linux.tar.gz", LINUX_SHA),
        ]:
            source = root / ".poster" / ("original-" + role) / filename
            actual = hashlib.sha256(source.read_bytes()).hexdigest()
            assert actual == expected, "ORIGINAL_PACKAGE_HASH_MISMATCH"
            proof["artifacts"][role] = dict(file=filename, sha256=actual, bytes=source.stat().st_size)
        bundle = private / "OriginalFull"
        with zipfile.ZipFile(root / ".poster/original-windows/poster-maker-windows.zip") as archive:
            archive.extractall(bundle)
        info = json.loads((bundle / "BUILD_INFO.json").read_bytes())
        assert info["commit"] == COMMIT
        # The archive hash is pinned before extracting or inspecting its manifest.
        release = private / "verified-linux"
        with tarfile.open(root / ".poster/original-linux/poster-linux.tar.gz") as archive:
            archive.extractall(release, filter="data")
        release = release / "poster-linux"
        manifest = json.loads((release / "release.json").read_bytes())
        assert manifest["commit"] == COMMIT and manifest["wheelhouse_python"] == "3.12"
        for filename, expected in manifest["files"].items():
            relative = Path(filename)
            assert not relative.is_absolute() and ".." not in relative.parts
            assert hashlib.sha256((release / relative).read_bytes()).hexdigest() == expected
        proof["linux_manifest"] = dict(
            commit=manifest["commit"],
            files_verified=len(manifest["files"]),
            wheelhouse_python=manifest["wheelhouse_python"],
            wheelhouse_platform=manifest["wheelhouse_platform"],
        )
        phase = "WSL2 kernel and isolation"
        execute([sys.executable, str(root / "scripts/crosshost/probe_wsl2.py")])
        probe = json.loads((root / ".poster/crosshost-infrastructure/probe.json").read_bytes())
        assert probe["status"] == "WSL2_READY"
        proof["infrastructure"] = probe
        keeper = subprocess.Popen(
            ["wsl.exe", "-d", DISTRO, "-u", "root", "--exec", "/bin/sleep", "infinity"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        phase = "ephemeral SSH bootstrap"
        key = Ed25519PrivateKey.generate()
        key_path = private / "ssh_ed25519"
        key_path.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.OpenSSH,
                serialization.NoEncryption(),
            )
        )
        restrict(key_path)
        pub = (
            key.public_key()
            .public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
            .decode()
        )
        guest = json.loads(
            wsl(
                ["/usr/bin/python3", "-c", BOOTSTRAP],
                data=json.dumps(dict(public_key=pub)).encode(),
                timeout=1000,
            )
        )
        assert guest["uid"] != 0
        ipaddress.IPv4Address(guest["ip"])
        hosts_line = guest["ip"] + " " + HOST + " # poster-crosshost-ephemeral"
        with hosts.open("a", encoding="utf-8") as stream:
            stream.write("\n" + hosts_line + "\n")
        known = private / "known_hosts"
        known.write_text(HOST + " " + guest["host_key"] + "\n", encoding="ascii")
        restrict(known)
        import base64

        host_digest = (
            base64.b64encode(hashlib.sha256(base64.b64decode(guest["host_key"].split()[1])).digest())
            .decode()
            .rstrip("=")
        )
        proof["ssh"] = dict(
            hostname=HOST,
            port=22,
            host_key_sha256="SHA256:" + host_digest,
            strict_host_key_checking=True,
            uid=guest["uid"],
            windows_drive_sharing=False,
            interop=False,
        )
        options = [
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "ConnectTimeout=15",
            "-o",
            "UserKnownHostsFile=" + str(known),
            "-i",
            str(key_path),
        ]
        destination = "postercheck@" + HOST

        def ssh(args, **kwargs):
            return execute(["ssh", "-T", *options, destination, shlex.join(args)], **kwargs)

        # Only two public files cross the boundary. No Controller workspace,
        # queue.key, private config or Windows drive is shared with the VM.
        phase = "pinned SSH file transfer and offline install"
        ssh(["mkdir", "-m", "700", GUEST_HOME + "/checks"])
        for source, target in [
            (root / ".poster/original-linux/poster-linux.tar.gz", GUEST_HOME + "/poster-linux.tar.gz"),
            (root / "scripts/crosshost/linux_peer.py", GUEST_HOME + "/checks/linux_peer.py"),
        ]:
            execute(["scp", *options, str(source), destination + ":" + target])
        ssh(["/usr/bin/python3.12", "-c", INSTALL], timeout=600)
        phase = "real Chromium dependencies and browser"
        wsl([PYTHON, "-m", "playwright", "install-deps", "chromium"], timeout=600)
        ssh([PYTHON, "-m", "playwright", "install", "chromium"], timeout=600)
        phase = "actual frozen Windows to Linux acceptance"
        config = dict(
            windows_bundle=str(bundle),
            expected_commit=COMMIT,
            ssh_destination=destination,
            ssh_key=str(key_path),
            known_hosts=str(known),
            linux_python=PYTHON,
            linux_helper=GUEST_HOME + "/checks/linux_peer.py",
            linux_root=GUEST_HOME + "/checks/run-001",
            relay_url="https://" + HOST + ":18443",
        )
        safe_json(private / "config.json", config)
        result = subprocess.run(
            [
                sys.executable,
                str(root / "scripts/check_remote_crosshost.py"),
                "--config",
                str(private / "config.json"),
                "--output",
                str(private / "run"),
            ],
            capture_output=True,
            timeout=1000,
        )
        report = json.loads((private / "run/report.json").read_bytes())
        # Actions truncates bodies at 5,000 characters and retains only ten
        # notices per step. Publish the verdict plus five bounded groups;
        # provenance below is the seventh notice. Full JSON remains in artifact.
        print(
            "::notice title=Cross-host 18-scenario report::"
            + json.dumps({k: v for k, v in report.items() if k != "proof"})
        )
        groups = dict(
            windows=("windows", "production_commit", "launchers"),
            linux=("linux", "parallel"),
            history=("jobs", "sqlite_history"),
            graph=("fixture", "snapshot", "returned_files", "tls"),
            lifecycle=("browser", "reconnect", "partition", "secret_boundary", "revoke", "stage5d_session"),
        )
        for name, fields in groups.items():
            value = {k: report["proof"][k] for k in fields if k in report.get("proof", {})}
            print("::notice title=Cross-host evidence " + name + "::" + json.dumps(value))
        shutil.copyfile(private / "run/report.json", public / "report.json")
        if (private / "run/windows-history-safe.sqlite").exists():
            shutil.copyfile(
                private / "run/windows-history-safe.sqlite", public / "windows-history-safe.sqlite"
            )
        if (private / "run/returned-files").exists():
            shutil.copytree(private / "run/returned-files", public / "returned-files")
        screenshot = private / "run/windows-controller.png"
        if screenshot.exists():
            shutil.copyfile(screenshot, public / screenshot.name)
        proof.update(
            status=report["status"],
            full_cross_host_acceptance=report["full_cross_host_acceptance"],
            scenario_counts={
                s: list(report["scenarios"].values()).count(s) for s in ("PASSED", "FAILED", "NOT_EXECUTED")
            },
            acceptance_phase=report.get("phase"),
            failure_line=report.get("failure_line"),
        )
        if result.returncode or report["status"] != "PASSED":
            raise RuntimeError("ACCEPTANCE_FAILED")
    except Exception as exc:
        proof.update(failed_phase=phase, error_type=type(exc).__name__)
        if phase == "actual frozen Windows to Linux acceptance":
            proof.update(status="FAILED", full_cross_host_acceptance=False)
        raise RuntimeError("Hosted cross-host failure at " + phase) from None
    finally:
        if keeper is not None:
            keeper.terminate()
        subprocess.run(["wsl.exe", "--terminate", DISTRO], capture_output=True, timeout=60)
        if hosts_line:
            hosts.write_text(hosts.read_text().replace(hosts_line, ""))
        (public / "provenance.json").write_text(json.dumps(proof, indent=2), encoding="utf-8")
        # No private file, full SQLite DB, token, journal or raw child log is uploaded.
        print("::notice title=Cross-host acceptance provenance::" + json.dumps(proof))


if __name__ == "__main__":
    main()
