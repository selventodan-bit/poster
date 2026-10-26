"""Adapter for the ORIGINAL local worker.execute and GraphRunner; no second executor."""

import argparse
import asyncio
import json
import os
from pathlib import Path
import threading

from poster.dispatcher.worker import Controls, execute


class ExecutionAdapter:
    def __init__(self, root, packet):
        self.root = root
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
        return True  # Controller durably marked RUNNING BEFORE releasing the key.

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
    controls = Controls(pending)
    try:
        value = json.loads(first)
        os.environ["POSTER_SESSION_KEY"] = value["key"]
        adapter = ExecutionAdapter(args.root, value)
        threading.Thread(target=controls.read, daemon=True).start()
        threading.Thread(target=controls.watchdog, daemon=True).start()
        return asyncio.run(execute(adapter, value["execution"], value["execution"]["owner"], controls))
    except Exception:
        return 2
    finally:
        controls.finished.set()


if __name__ == "__main__":
    raise SystemExit(main())
