"""Adapter for the ORIGINAL local worker.execute and GraphRunner; no second executor."""

import argparse
import asyncio
import json
import os
from pathlib import Path
import threading
import time

from poster.dispatcher.worker import Controls, execute
from .protocol import LEASE


class LeaseControls(Controls):
    def __init__(self, pending, expires):
        super().__init__(pending)
        self.expires = expires

    def read(self):
        pending = self.pending
        try:
            while True:
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    if line.startswith(b"LEASE "):
                        expiry = float(line[6:])
                        if expiry <= time.time() + LEASE + 3:
                            self.expires = max(self.expires, expiry)
                    elif line in {b"CANCEL_REQUESTED", b"TIMEOUT"}:
                        self.request(line.decode())
                chunk = os.read(0, 4096)
                if not chunk or len(pending) > 4096:
                    self.request("DISPATCHER_LOST")
                    return
                pending += chunk
        except Exception:
            self.request("DISPATCHER_LOST")

    def lease_watchdog(self):
        while not self.finished.wait(0.05):
            if time.time() >= self.expires:
                self.request("DISPATCHER_LOST")
                return


class ExecutionAdapter:
    def __init__(self, root, packet, controls):
        self.root = root
        self.controls = controls
        self.job = packet["job"]
        from .protocol import un64

        self.job["inputs_cipher"] = un64(self.job["inputs_cipher"])
        self.current = packet["execution"]
        self.current.update(effects_possible=0, last_confirmed_step=None)

    def payload(self, _):
        return self.job

    def execution(self, _):
        return self.current

    def start(self, *_):
        return not self.controls.reason and time.time() < self.controls.expires

    def progress(self, run_id, *, confirmed=None, effects=False):
        self.current["effects_possible"] |= int(effects)
        if confirmed:
            self.current["last_confirmed_step"] = confirmed
        print(json.dumps(dict(kind="progress", confirmed=confirmed, effects=effects)), flush=True)

    def note(self, *_):
        pass

    def complete(self, run_id, status, **value):
        print(
            json.dumps(
                dict(
                    kind="complete", status=status, report=str(value["report"]), sha256=value["report_sha256"]
                )
            ),
            flush=True,
        )
        return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    raw = b""
    while b"\n" not in raw and len(raw) < 2_000_000:
        chunk = os.read(0, 4096)
        if not chunk:
            return 2
        raw += chunk
    if b"\n" not in raw:
        return 2
    first, pending = raw.split(b"\n", 1)
    controls = None
    try:
        value = json.loads(first)
        os.environ["POSTER_SESSION_KEY"] = value["key"]
        controls = LeaseControls(pending, value["lease"])
        adapter = ExecutionAdapter(args.root, value, controls)
        threading.Thread(target=controls.lease_watchdog, daemon=True).start()
        threading.Thread(target=controls.read, daemon=True).start()
        threading.Thread(target=controls.watchdog, daemon=True).start()
        return asyncio.run(execute(adapter, value["execution"], value["execution"]["owner"], controls))
    except Exception:
        return 2
    finally:
        if controls:
            controls.finished.set()


if __name__ == "__main__":
    raise SystemExit(main())
