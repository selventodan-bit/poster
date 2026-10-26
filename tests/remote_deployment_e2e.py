"""Install actual pinned wheels, run TLS/services/GraphRunner, preserve durable state.

Linux/Linux evidence only. POSTER_TEST_SYSTEMD=1 additionally uses the real
system manager of an ephemeral CI VM, always with User=<ordinary CI user>.
"""

import getpass
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile

import pytest

from poster.dispatcher.host import ensure_service, request
from poster.dispatcher.models import DispatcherError
from poster.remote.registry import read_json
from poster.remote.setup import tls_files, relay_profiles
from test_dispatcher_service import wait

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts/install_remote.py"


def invoke(*args, cwd=None, input=None):
    env = {k: v for k, v in os.environ.items() if k not in {"PYTHONPATH", "PYTHONHOME"}}
    value = subprocess.run(list(map(str, args)), cwd=cwd, env=env, input=input, capture_output=True)
    assert value.returncode == 0, (list(map(str, args)), value.stdout[-2000:], value.stderr[-2000:])
    return value


def extract(archive, target):
    with tarfile.open(archive) as tar:
        tar.extractall(target, filter="data")
    return target / "poster-linux"


@pytest.fixture
def deployment():
    archive = Path(os.environ["POSTER_LINUX_RELEASE"])
    systemd = os.environ.get("POSTER_TEST_SYSTEMD") == "1"
    if sys.platform != "linux" or os.geteuid() == 0:
        pytest.fail("Requires real Linux and ordinary UID")
    # PrivateTmp in the system unit deliberately hides host /tmp.
    parent = ROOT / ".poster/deployment-tests"
    parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=parent) as temporary:
        root = Path(temporary)
        release = extract(archive, root / "new")
        previous = os.environ.get("POSTER_PREVIOUS_LINUX_RELEASE")
        prior = extract(previous, root / "old") if previous else release
        programs, states, processes = {}, {}, {}
        units = root / "units"
        units.mkdir()

        def install(role, source=release):
            extra = ["--service-user", getpass.getuser()] if systemd else []
            return invoke(
                sys.executable,
                INSTALLER,
                "install",
                "--release",
                source,
                "--program",
                programs[role],
                "--state",
                states[role],
                "--role",
                role,
                "--unit-dir",
                units,
                "--offline",
                *extra,
            )

        def manage(role, action):
            if systemd:
                invoke("sudo", "systemctl", action, "poster-" + role + ".service")
            elif action in {"stop", "restart"}:
                process = processes.pop(role)
                process.terminate()
                assert process.wait(45) == 0
            if not systemd and action in {"start", "restart"}:
                env = {k: v for k, v in os.environ.items() if k not in {"PYTHONPATH", "PYTHONHOME"}}
                processes[role] = subprocess.Popen(
                    [
                        str(programs[role] / "current/bin/python"),
                        "-m",
                        "poster.remote",
                        "serve",
                        "--role",
                        role,
                        "--root",
                        str(states[role]),
                    ],
                    cwd=states[role],
                    env=env,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )

        def ready(role):
            def check():
                value = subprocess.run(
                    [
                        str(programs[role] / "current/bin/python"),
                        "-m",
                        "poster.remote",
                        "status",
                        "--root",
                        str(states[role]),
                        "--role",
                        role,
                    ],
                    cwd=states[role],
                    capture_output=True,
                )
                return json.loads(value.stdout) if value.returncode == 0 else None

            return wait(check)

        controller = root / "Controller"
        ensure_service(controller)
        try:
            for role in ("relay", "agent"):
                programs[role], states[role] = root / (role + "-program"), root / (role + "-state")
                install(role, prior)
                assert (programs[role] / "current").is_symlink()
                # No source checkout or GUI dependency in the installed runtime.
                value = invoke(
                    programs[role] / "current/bin/python",
                    "-c",
                    "import poster,importlib.util; print(poster.__file__); "
                    "assert importlib.util.find_spec('PySide6') is None",
                    cwd=root,
                )
                assert b"site-packages" in value.stdout
                invoke("systemd-analyze", "verify", units / ("poster-" + role + ".service"))
            relay = states["relay"]
            cert, _ = tls_files(relay, "127.0.0.1")
            import socket

            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            url = f"https://127.0.0.1:{port}"
            relay_profiles(relay, url, cert)
            (relay / "relay-service.json").write_text(json.dumps(dict(host="127.0.0.1", port=port)))
            if systemd:
                for role in ("relay", "agent"):
                    invoke(
                        "sudo",
                        "install",
                        "-m",
                        "644",
                        units / ("poster-" + role + ".service"),
                        "/etc/systemd/system/poster-" + role + ".service",
                    )
                invoke("sudo", "systemctl", "daemon-reload")
            manage("relay", "start")
            ready("relay")
            public = request(
                controller,
                "remote_pairing_export",
                operator_file=str(relay / "pairing-relay.json"),
                url=url,
                destination=str(root / "pairing.json"),
            )
            value = invoke(
                programs["agent"] / "current/bin/python",
                "-m",
                "poster.remote",
                "pair",
                "--root",
                states["agent"],
                "--name",
                "Installed Linux",
                "--offer",
                root / "pairing.json",
                "--expect-controller",
                public["controller"],
                cwd=root,
            )
            agent_id = read_json(states["agent"] / "registration.json")["peer"]["id"]
            request(
                controller,
                "remote_pairing_finish",
                registration=str(states["agent"] / "registration.json"),
                fingerprint=agent_id,
            )
            assert request(controller, "remote_agents")[0]["state"] == "PENDING"
            request(controller, "remote_approve", id=agent_id, fingerprint=agent_id)
            manage("agent", "start")
            ready("agent")
            wait(lambda: request(controller, "remote_agents")[0]["online"])
            if systemd:
                for role in ("agent", "relay"):
                    invoke("sudo", "systemctl", "enable", "poster-" + role + ".service")
                    invoke("sudo", "systemctl", "is-enabled", "poster-" + role + ".service")
                    pid = int(
                        invoke(
                            "sudo", "systemctl", "show", "-p", "MainPID", "--value", "poster-" + role
                        ).stdout
                    )
                    import psutil

                    assert psutil.Process(pid).uids().effective == os.geteuid()
            yield dict(
                root=root,
                release=release,
                programs=programs,
                states=states,
                install=install,
                manage=manage,
                ready=ready,
                controller=controller,
                agent_id=agent_id,
                units=units,
                systemd=systemd,
            )
        finally:
            try:
                request(controller, "shutdown")
            except DispatcherError:
                pass
            if systemd:
                for role in ("agent", "relay"):
                    subprocess.run(
                        ["sudo", "systemctl", "disable", "--now", "poster-" + role], capture_output=True
                    )
                    subprocess.run(
                        ["sudo", "rm", "-f", "/etc/systemd/system/poster-" + role + ".service"], check=True
                    )
                invoke("sudo", "systemctl", "daemon-reload")
            else:
                for process in processes.values():
                    if process.poll() is None:
                        process.terminate()
                        process.wait(45)


def test_installed_services_graph_stop_restart_update_identity_journal_and_remove(deployment):
    p = deployment
    root, agent = p["controller"], p["states"]["agent"]

    def check_job():
        job = request(root, "remote_test", id=p["agent_id"])["job"]
        value = wait(lambda: request(root, "remote_test_status", job=job)["file_returned"])
        assert value and request(root, "show", id=job)["status"] == "SUCCEEDED"
        return request(root, "show", id=job)["history"][0]["run_id"]

    run_id = check_job()
    identity = (agent / "agent.json").read_bytes()
    p["manage"]("agent", "stop")
    wait(lambda: not request(root, "remote_agents")[0]["online"])
    assert "REMOTE_STOPPED" in request(root, "remote_diagnose", id=p["agent_id"])["codes"]
    import sqlite3

    with sqlite3.connect(agent / "agent-journal.sqlite") as db:
        assert db.execute("SELECT state FROM runs WHERE id=?", (run_id,)).fetchone()[0] == "DONE"
    journal = hashlib.sha256((agent / "agent-journal.sqlite").read_bytes()).hexdigest()
    for role in ("agent", "relay"):
        if role == "relay":
            p["manage"](role, "stop")
        p["install"](role)
        assert (p["programs"][role] / "current").resolve().name == read_json(p["release"] / "release.json")[
            "commit"
        ]
    assert identity == (agent / "agent.json").read_bytes()
    assert hashlib.sha256((agent / "agent-journal.sqlite").read_bytes()).hexdigest() == journal
    p["manage"]("relay", "start")
    p["manage"]("agent", "start")
    p["ready"]("relay")
    p["ready"]("agent")
    wait(lambda: request(root, "remote_agents")[0]["online"])
    # Old diagnostic sources must never affect the promised safe next check.
    next((root / "connection-check").rglob("*.postergraph.json")).write_text("{}")
    assert check_job() != run_id
    # An active journal lock refuses update; no identity change or implicit second process.
    result = subprocess.run(
        [
            sys.executable,
            str(INSTALLER),
            "install",
            "--release",
            str(p["release"]),
            "--program",
            str(p["programs"]["agent"]),
            "--state",
            str(agent),
            "--role",
            "agent",
            "--unit-dir",
            str(p["units"]),
            "--offline",
        ],
        capture_output=True,
    )
    assert result.returncode != 0 and identity == (agent / "agent.json").read_bytes()
    p["manage"]("agent", "stop")
    # Damaged journal must not activate a release or create a new journal/identity.
    saved = (agent / "agent-journal.sqlite").read_bytes()
    (agent / "agent-journal.sqlite").write_bytes(b"damaged journal")
    result = subprocess.run(
        [
            sys.executable,
            str(INSTALLER),
            "install",
            "--release",
            str(p["release"]),
            "--program",
            str(p["programs"]["agent"]),
            "--state",
            str(agent),
            "--role",
            "agent",
            "--unit-dir",
            str(p["units"]),
            "--offline",
        ],
        capture_output=True,
    )
    assert result.returncode != 0
    assert (agent / "agent-journal.sqlite").read_bytes() == b"damaged journal"
    assert identity == (agent / "agent.json").read_bytes()
    (agent / "agent-journal.sqlite").write_bytes(saved)
    p["manage"]("relay", "stop")
    if p["systemd"]:
        for role in ("agent", "relay"):
            invoke(
                sys.executable,
                INSTALLER,
                "service",
                "--action",
                "remove",
                "--role",
                role,
                "--unit-dir",
                "/etc/systemd/system",
                "--system",
            ) if os.geteuid() == 0 else invoke(
                "sudo",
                sys.executable,
                INSTALLER,
                "service",
                "--action",
                "remove",
                "--role",
                role,
                "--unit-dir",
                "/etc/systemd/system",
                "--system",
            )
    assert (
        identity == (agent / "agent.json").read_bytes()
        and (agent / "agent-journal.sqlite").read_bytes() == saved
    )
    # Only public proof is uploaded; temporary private profiles are never CI artifacts.
    report = dict(
        result="PASSED",
        endpoints="Linux/Linux",
        systemd=p["systemd"],
        non_root=True,
        installed_wheels=True,
        returned_file=True,
        stop_restart=True,
        previous_commit=os.environ.get("POSTER_PREVIOUS_COMMIT"),
        current_commit=read_json(p["release"] / "release.json")["commit"],
        identity_preserved=True,
        journal_preserved=True,
        corrupt_journal_refused=True,
    )
    target = ROOT / ".poster/remote-deployment-report.json"
    target.write_text(json.dumps(report, indent=2))
    print("::notice title=Remote deployment::" + json.dumps(report))
