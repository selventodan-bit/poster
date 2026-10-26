"""Foreground notebook child. EOF from its owning kernel stops the original Host.

Local authenticated IPC only: no Relay, Agent, SSH, network server or keepalive.
"""

import os
from pathlib import Path
import sys
import threading

from poster.dispatcher.host import Host, request
from poster.dispatcher.security import private_dir


def main():
    root = private_dir(Path(sys.argv[1]))
    host = Host(root)

    def eof():
        try:
            while os.read(0, 1024):
                pass
        finally:
            # Request on the RPC thread which owns SQLite. The kernel pipe is
            # lifetime control, never a transport for OAuth credentials.
            try:
                request(root, "shutdown")
            except Exception:
                host.done.set()

    threading.Thread(target=eof, daemon=True, name="kernel-lifetime").start()
    host.serve()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
