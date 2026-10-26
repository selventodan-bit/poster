"""Trusted SSH test helper, not a network API or a production Agent alternative.

Invoked only by check_remote_crosshost.py over authenticated operator SSH.
Fixed actions, private stdin/stdout file channel; never run with shell tracing.
"""

import argparse
import base64
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.metadata import version as package_version
import json
import os
from pathlib import Path
import platform
import re
import signal
import sqlite3
import subprocess
import sys
import threading
import time

import psutil

from poster.dispatcher.processes import same_process, terminate_tree
from poster.dispatcher.security import safe_json
from poster.remote.registry import read_json


def token(value):
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", value):
        raise ValueError("Invalid test label")
    return value


def fixture(root):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"<html><body>Poster cross-host fixture</body></html>")

        def do_POST(self):
            label = token(self.path.strip("/"))
            length = int(self.headers.get("Content-Length", 0))
            if not 0 <= length <= 65536:
                self.send_error(413)
                return
            body = self.rfile.read(length)
            # Store hashes/types only; never save protected input plaintext.
            try:
                parsed = json.loads(body)
                digest = hashlib.sha256(json.dumps(parsed, sort_keys=True).encode()).hexdigest()
                types = {k: type(v).__name__ for k, v in parsed.items()}
            except (ValueError, AttributeError):
                digest, types = hashlib.sha256(body).hexdigest(), {}
            with self.server.guard:
                with (root / "calls.jsonl").open("a") as stream:
                    stream.write(json.dumps(dict(label=label, sha256=digest, types=types)) + "\n")
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline and not (root / ("release-" + label)).exists():
                time.sleep(0.05)
            try:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"ok":true}')
            except OSError:
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads, server.guard = True, threading.Lock()
    safe_json(root / "fixture.json", dict(url=f"http://127.0.0.1:{server.server_port}"))
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        server.serve_forever()
    finally:
        server.server_close()


def launch(root, role):
    record = root / (role + "-process.json")
    if record.exists():
        old = read_json(record)
        if same_process(old["pid"], old["birth"]):
            raise ValueError("Already running")
    args = (
        [sys.executable, str(Path(__file__).resolve()), "--root", str(root), "fixture"]
        if role == "fixture"
        else [sys.executable, "-m", "poster.remote", "serve", "--root", str(root / role), "--role", role]
    )
    child = subprocess.Popen(
        args,
        cwd=root,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    safe_json(record, dict(pid=child.pid, birth=psutil.Process(child.pid).create_time()))


def stop(root, role, *, crash=False):
    path = root / (role + "-process.json")
    if path.exists():
        value = read_json(path)
        process = same_process(value["pid"], value["birth"])
        if process:
            if crash:
                process.kill()
            else:
                process.terminate()
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                current = same_process(value["pid"], value["birth"])
                try:
                    if not current or current.status() == psutil.STATUS_ZOMBIE:
                        break
                except psutil.NoSuchProcess:
                    break  # Exit after birth check is confirmed exit, not a failure.
                time.sleep(0.05)
            else:
                terminate_tree(value["pid"], value["birth"], group=True)


def inventory(root):
    owned = read_json(root / "observed.json") if (root / "observed.json").exists() else {}
    journal = root / "agent/agent-journal.sqlite"
    if journal.exists():
        with sqlite3.connect(journal.as_uri() + "?mode=ro", uri=True) as db:
            run_ids = {row[0] for row in db.execute("SELECT id FROM runs")}
            for pid, birth in db.execute("SELECT pid,birth FROM runs WHERE pid IS NOT NULL"):
                process = same_process(pid, birth)
                owned[str(pid)] = birth
                if process:
                    try:
                        for child in process.children(recursive=True):
                            owned[str(child.pid)] = child.create_time()
                    except psutil.NoSuchProcess:
                        pass
        # Chromium can reparent before the next SSH observation. The Agent's
        # existing per-run process marker also identifies these owned descendants.
        for process in psutil.process_iter():
            try:
                if (
                    process.pid != os.getpid()
                    and process.uids().real == os.geteuid()
                    and process.environ().get("POSTER_DISPATCHER_RUN") in run_ids
                ):
                    owned[str(process.pid)] = process.create_time()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
    safe_json(root / "observed.json", owned)
    alive = []
    for pid, birth in owned.items():
        process = same_process(int(pid), birth)
        try:
            if process and process.status() != psutil.STATUS_ZOMBIE:
                alive.append(dict(pid=int(pid), birth=birth, name=process.name()))
        except psutil.NoSuchProcess:
            pass  # A process may exit between the two read-only observations.
    return dict(observed=len(owned), alive=alive)


def action(root, name, data):
    if name == "prepare":
        from poster.remote.setup import tls_files, relay_profiles

        root.mkdir(parents=True, mode=0o700, exist_ok=False)
        safe_json(root / "owner.json", dict(session=token(data["session"])))
        cert, _ = tls_files(root / "relay", data["hostname"])
        relay_profiles(root / "relay", data["url"], cert)
        safe_json(root / "relay/relay-service.json", dict(host="0.0.0.0", port=int(data["port"])))
        launch(root, "relay")
        launch(root, "fixture")
        # This private profile is piped straight into a private Windows file.
        return read_json(root / "relay/pairing-relay.json")
    if read_json(root / "owner.json")["session"] != data.get("session"):
        raise ValueError("Test session does not own this root")
    if name == "pair":
        from poster.remote.pairing import agent_pair

        offer = root / "pairing.json"
        safe_json(offer, data["offer"])
        peer = agent_pair(root / "agent", "Cross-host Linux", offer, data["controller"])
        offer.unlink()
        return dict(registration=read_json(root / "agent/registration.json"), fingerprint=peer)
    if name in {"start", "stop", "kill"}:
        if data["role"] not in {"agent", "relay"}:
            raise ValueError("Invalid role")
        if name == "start":
            launch(root, data["role"])
        else:
            inventory(root)
            stop(root, data["role"], crash=name == "kill")
        return dict(ok=True)
    if name == "release":
        (root / ("release-" + token(data["label"]))).touch(mode=0o600)
        return dict(ok=True)
    if name == "observe":
        info = read_json(root / "fixture.json") if (root / "fixture.json").exists() else {}
        info["calls"] = (
            [json.loads(line) for line in (root / "calls.jsonl").read_text().splitlines()]
            if (root / "calls.jsonl").exists()
            else []
        )
        info["processes"] = inventory(root)
        if (root / "agent/agent.json").exists():
            config = (root / "agent/agent.json").read_bytes()
            info["identity_sha256"] = hashlib.sha256(config).hexdigest()
        info["platform"] = sys.platform
        info["uid"] = os.geteuid()
        info["program_commit"] = Path(sys.executable).resolve().parent.parent.name
        info["helper_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        info["os"] = dict(
            system=platform.system(),
            kernel=platform.release(),
            arch=platform.machine(),
            distribution=platform.freedesktop_os_release().get("PRETTY_NAME"),
            boot_id=Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
            python=platform.python_version(),
        )
        info["packages"] = {
            name: package_version(name)
            for name in (
                "poster-http",
                "httpx",
                "cryptography",
                "psutil",
                "playwright",
                "pydantic",
                "PyYAML",
                "filelock",
            )
        }
        info["roles"] = {}
        for role in ("agent", "relay", "fixture"):
            path = root / (role + "-process.json")
            if path.exists():
                record = read_json(path)
                process = same_process(record["pid"], record["birth"])
                info["roles"][role] = dict(
                    **record, alive=bool(process and process.status() != psutil.STATUS_ZOMBIE)
                )
        return info
    if name == "bundle":
        run, version = data["run"], data["version"]
        if not re.fullmatch("[a-f0-9]{32}", run) or not re.fullmatch("[a-f0-9]{64}", version):
            raise ValueError("Invalid evidence ID")
        directory = root / "agent/runs" / run / "snapshots" / version
        return dict(
            bundle=base64.b64encode((directory / "bundle.enc").read_bytes()).decode(),
            manifest=read_json(directory / "manifest.json"),
        )
    if name == "scan":
        # Compare in Windows. Master queue key is never sent to this machine.
        files, total = {}, 0
        for path in (root / "agent").rglob("*"):
            if path.is_file() and not path.is_symlink():
                raw = path.read_bytes()
                total += len(raw)
                if total > 40_000_000:
                    raise ValueError("Private evidence too large")
                files[path.relative_to(root / "agent").as_posix()] = base64.b64encode(raw).decode()
        return dict(files=files)
    if name == "cleanup":
        for role in ("agent", "relay", "fixture"):
            stop(root, role)
        observed = inventory(root)
        for entry in observed["alive"]:
            terminate_tree(entry["pid"], entry["birth"], group=False)
        return dict(stopped=True)
    raise ValueError("Unsupported fixed test action")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument(
        "action",
        choices=(
            "prepare",
            "pair",
            "start",
            "stop",
            "kill",
            "release",
            "observe",
            "bundle",
            "scan",
            "cleanup",
            "fixture",
        ),
    )
    args = parser.parse_args()
    os.umask(0o077)
    if sys.platform != "linux" or os.geteuid() == 0 or not args.root.is_absolute():
        raise SystemExit("Requires trusted non-root Linux and an absolute fresh test root")
    if args.action == "fixture":
        fixture(args.root)
        return
    try:
        raw = sys.stdin.buffer.read(2_000_001)
        if len(raw) > 2_000_000:
            raise ValueError("Input limit")
        result = action(args.root, args.action, json.loads(raw or b"{}"))
        sys.stdout.write(json.dumps(result))
    except Exception as exc:
        # Exception text may contain private data: only its class crosses the diagnostic channel.
        import traceback

        sys.stderr.write(
            json.dumps(
                dict(
                    error_type=type(exc).__name__,
                    frames=[
                        Path(f.filename).name + ":" + str(f.lineno)
                        for f in traceback.extract_tb(exc.__traceback__)[-5:]
                    ],
                )
            )
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
