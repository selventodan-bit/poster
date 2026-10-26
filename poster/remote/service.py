"""Unprivileged deployment entry points and payload-free process readiness."""

import os
from pathlib import Path
import signal
import time

import psutil
from filelock import FileLock

from poster.dispatcher.models import DispatcherError
from poster.dispatcher.processes import same_process
from poster.dispatcher.security import private_dir, safe_json
from .diagnostics import doctor
from .registry import read_json


def serve(root, role):
    if os.name != "posix" or os.geteuid() == 0:
        raise DispatcherError("REMOTE_SERVICE")
    root = private_dir(root)
    check = doctor(root, role)
    if not check["ready"]:
        raise DispatcherError(next(iter(check["codes"]), "REMOTE_CONFIG"))
    if role == "agent":
        from .agent import Agent

        agent = Agent(root)
        signal.signal(signal.SIGTERM, lambda *_: setattr(agent, "done", True))
        signal.signal(signal.SIGINT, lambda *_: setattr(agent, "done", True))
        agent.run()
    else:
        from .relay import server

        options = read_json(root / "relay-service.json")
        with FileLock(str(root / "relay-service.lock"), timeout=0):
            instance = server(
                read_json(root / "relay.json"),
                root / "mailbox.sqlite",
                root / "tls.crt",
                root / "tls.key",
                host=options["host"],
                port=options["port"],
            )
            stopped = False

            def stop(*_):
                nonlocal stopped
                stopped = True

            signal.signal(signal.SIGTERM, stop)
            signal.signal(signal.SIGINT, stop)
            instance.timeout = 0.3
            try:
                while not stopped:
                    safe_json(
                        root / "status.json",
                        dict(
                            pid=os.getpid(),
                            birth=psutil.Process().create_time(),
                            at=time.time(),
                            phase="RUNNING",
                            role=role,
                        ),
                    )
                    instance.handle_request()
            finally:
                instance.server_close()
                safe_json(
                    root / "status.json",
                    dict(
                        pid=os.getpid(),
                        birth=psutil.Process().create_time(),
                        at=time.time(),
                        phase="STOPPED",
                        role=role,
                    ),
                )


def status(root, role):
    result = doctor(root, role)
    try:
        value = read_json(Path(root) / "status.json")
        process = same_process(value["pid"], value["birth"])
        result["running"] = bool(
            process
            and process.status() != psutil.STATUS_ZOMBIE
            and value["phase"] == "RUNNING"
            and time.time() - value["at"] < 10
        )
    except (OSError, ValueError, KeyError):
        result["running"] = False
    result["ready"] &= result["running"]
    return result
