"""Real frozen Windows + separate trusted Linux over verified TLS. No emulation.

Without infrastructure use --not-executed; this produces evidence of the missing
criterion, never a successful cross-host acceptance. Private config stays local.
"""

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shlex
import socket
import sqlite3
import subprocess
import time
import traceback
from urllib.parse import urlsplit
import uuid

SCENARIOS = (
    "frozen_windows_processes",
    "linux_processes",
    "tls_hostname_certificate_auth",
    "pairing_fingerprints_approval",
    "signed_heartbeat",
    "exact_maker_snapshot",
    "typed_and_protected_inputs",
    "linux_graphrunner_output",
    "windows_history",
    "two_parallel_executions",
    "global_and_project_limits",
    "cancellation",
    "browser_timeout_cleanup",
    "agent_disconnect_reconnect",
    "post_partition_unknown_no_resend",
    "secret_and_queue_key_boundary",
    "no_orphan_browsers",
    "revoke_without_fallback",
)


def report(path, *, status, scenarios=None, **evidence):
    if status not in {"PASSED", "FAILED", "NOT_EXECUTED"}:
        raise ValueError("Invalid acceptance state")
    if status == "PASSED" and (
        any((scenarios or {}).get(s) != "PASSED" for s in SCENARIOS)
        or not all(
            evidence.get(k) is True
            for k in ("windows_frozen", "linux_non_root", "separate_hosts", "real_tcp_tls")
        )
    ):
        raise ValueError("Incomplete cross-host evidence")
    value = dict(
        format="poster.crosshost.v1",
        status=status,
        full_cross_host_acceptance=status == "PASSED",
        scenarios={s: (scenarios or {}).get(s, "NOT_EXECUTED") for s in SCENARIOS},
        **evidence,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2), encoding="utf-8")
    return value


def wait(predicate, timeout=65):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.2)
    raise AssertionError("Cross-host deadline")


def export_history(source, output):
    """Keep a private backup and a separate SQLite allowlist export for review."""
    with sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True) as db:
        with sqlite3.connect(output / "windows-history-private.sqlite") as backup:
            db.backup(backup)
        db.row_factory = sqlite3.Row
        executions = [
            dict(row)
            for row in db.execute(
                "SELECT run_id,job_id,attempt,status,pid,birth,leased,started,finished,code,effects_possible "
                "FROM executions ORDER BY leased"
            )
        ]
        jobs = [dict(row) for row in db.execute("SELECT id,status,attempt FROM jobs ORDER BY created")]
    with sqlite3.connect(output / "windows-history-safe.sqlite") as safe:
        safe.execute("CREATE TABLE jobs(id TEXT,status TEXT,attempt INTEGER)")
        safe.execute(
            "CREATE TABLE executions(run_id TEXT,job_id TEXT,attempt INTEGER,status TEXT,"
            "pid INTEGER,birth REAL,leased REAL,started REAL,finished REAL,code TEXT,effects_possible INTEGER)"
        )
        safe.executemany("INSERT INTO jobs VALUES(:id,:status,:attempt)", jobs)
        safe.executemany(
            "INSERT INTO executions VALUES(:run_id,:job_id,:attempt,:status,:pid,:birth,"
            ":leased,:started,:finished,:code,:effects_possible)",
            executions,
        )
    return dict(sqlite_history=executions, jobs=jobs)


class Peer:
    def __init__(self, config):
        self.config = config
        self.last_failure = None
        self.session = uuid.uuid4().hex
        if not re.fullmatch(r"[a-z_][a-z0-9_-]*@[a-zA-Z0-9.-]+", config["ssh_destination"]):
            raise ValueError("Invalid SSH destination")
        for field in ("linux_python", "linux_helper", "linux_root"):
            if not config[field].startswith("/") or "\n" in config[field]:
                raise ValueError("Absolute Linux paths required")

    def call(self, action, **payload):
        # Only the fixed trusted helper/action crosses the SSH command channel.
        # Tokens and offers use a private stdin/stdout pipe, never argv or URL.
        remote = shlex.join(
            [
                self.config["linux_python"],
                self.config["linux_helper"],
                "--root",
                self.config["linux_root"],
                action,
            ]
        )
        value = subprocess.run(
            [
                "ssh",
                "-T",
                "-o",
                "BatchMode=yes",
                "-o",
                "StrictHostKeyChecking=yes",
                "-o",
                "IdentitiesOnly=yes",
                "-o",
                "ConnectTimeout=15",
                "-o",
                "UserKnownHostsFile=" + str(Path(self.config["known_hosts"]).resolve()),
                "-i",
                str(Path(self.config["ssh_key"]).resolve()),
                self.config["ssh_destination"],
                remote,
            ],
            input=json.dumps(dict(payload, session=self.session)).encode(),
            capture_output=True,
            timeout=120,
        )
        if value.returncode or len(value.stdout) > 60_000_000:
            self.last_failure = dict(action=action, returncode=value.returncode)
            try:
                detail = json.loads(value.stderr)
                if re.fullmatch("[A-Za-z]{1,80}", detail.get("error_type", "")) and all(
                    re.fullmatch("[A-Za-z_]+\.py:[0-9]+", f) for f in detail.get("frames", [])
                ):
                    self.last_failure.update(detail)
            except (ValueError, TypeError):
                pass
            raise RuntimeError("Trusted SSH helper failed")  # Never print private stdout/stderr.
        return json.loads(value.stdout)


def check(config, output):
    if os.name != "nt":
        raise RuntimeError("Requires a real Windows host; Linux cannot attest frozen Windows")
    import psutil
    from cryptography.fernet import Fernet
    from pywinauto import Desktop
    from poster.dispatcher.host import request
    from poster.dispatcher.models import DispatcherError
    from poster.dispatcher.security import private_dir, safe_json
    from poster.remote.relay import Client
    from poster.maker.example_projects import chain
    from poster.maker.model import Variable
    from poster.maker.storage import Vault, save_graph

    if output.exists():
        raise ValueError("Use a fresh private output directory")
    output = private_dir(output)
    bundle = Path(config["windows_bundle"]).resolve()
    info = json.loads((bundle / "BUILD_INFO.json").read_bytes())
    assert info["commit"] == config["expected_commit"]
    for role in ("service", "gui"):
        item = info["launchers"][role]
        assert hashlib.sha256((bundle / item["file"]).read_bytes()).hexdigest() == item["sha256"]
    peer, root = Peer(config), output / "Controller"
    env = {k: v for k, v in os.environ.items() if k not in {"PYTHONPATH", "PYTHONHOME", "POSTER_SESSION_KEY"}}
    env.update(
        POSTER_DISPATCHER_WORKSPACE=str(root),
        POSTER_DISPATCHER_OBSERVER=str(output / "observer-private.json"),
    )
    env["PATH"] = os.pathsep.join(
        [str(Path(os.environ["SystemRoot"]) / "System32"), os.environ["SystemRoot"]]
    )
    processes, passed, phase, prepared = [], {}, "prepare", False
    proof = dict(
        windows=dict(os=platform.system(), release=platform.release(), version=platform.version()),
        production_commit=info["commit"],
        launchers=info["launchers"],
        jobs=[],
        returned_files=[],
    )
    canary = "CROSSHOST_PRIVATE_" + uuid.uuid4().hex

    def ok(*names):
        for name in names:
            passed[name] = "PASSED"

    def call(op, **values):
        return request(root, op, **values)

    def online():
        return any(a["online"] for a in call("remote_agents"))

    def terminal(job):
        return wait(
            lambda: (
                v if (v := call("show", id=job))["status"] not in {"PENDING", "LEASED", "RUNNING"} else None
            )
        )

    def enqueue(actions, *, variables=None, inputs=None, secrets=None, timeout=60, outputs=None):
        graph = chain("Cross-host fixture", actions)
        graph.variables.update(variables or {})
        path = output / (graph.id + ".postergraph.json")
        save_graph(path, graph, Vault())
        original = path.read_bytes()
        item = call("add", source=str(path))
        value = call(
            "enqueue_batch",
            items=[
                dict(
                    id=item["id"],
                    agent_id=agent_id,
                    inputs=inputs or {},
                    secrets=secrets or {},
                    timeout=timeout,
                    remote_outputs=outputs or [],
                )
            ],
            request_id=graph.id,
        )
        assert not value["errors"]
        return value["jobs"][0], item, path, original

    try:
        url = urlsplit(config["relay_url"])
        assert (
            url.scheme == "https" and url.hostname and not url.username and not url.query and not url.fragment
        )
        prepared = True  # Cleanup is safe even after a partially failed preparation.
        operator = peer.call("prepare", hostname=url.hostname, url=config["relay_url"], port=url.port or 443)
        safe_json(output / "operator-private.json", operator)
        processes.append(
            subprocess.Popen([str(bundle / "PosterService.exe"), "--workspace", str(root)], env=env)
        )

        def ping():
            try:
                return call("ping")
            except DispatcherError:
                return None

        wait(ping)
        processes.append(subprocess.Popen([str(bundle / "PosterDispatcher.exe")], env=env))
        window = Desktop(backend="win32").window(title="Poster Dispatcher — очередь проектов")
        window.wait("visible", timeout=60)
        for process, name in zip(processes, ("PosterService.exe", "PosterDispatcher.exe"), strict=True):
            assert Path(psutil.Process(process.pid).exe()).resolve() == bundle / name
        proof["windows"]["processes"] = [
            dict(pid=p.pid, birth=psutil.Process(p.pid).create_time(), executable=name)
            for p, name in zip(processes, ("PosterService.exe", "PosterDispatcher.exe"), strict=True)
        ]
        ok("frozen_windows_processes")
        phase = "TLS and pairing"
        client = Client(operator["controller"], "controller")

        def relay_ready():
            try:
                return client.probe()["tls_verified"]
            except DispatcherError:
                return False

        wait(relay_ready)
        client.close()
        for bad, code in [
            ({**operator["controller"], "token": "bad-credential-" * 4}, "REMOTE_AUTH"),
            ({**operator["controller"], "ca_pem": None}, "REMOTE_TLS"),
            (
                {
                    **operator["controller"],
                    "url": config["relay_url"].replace(url.hostname, socket.gethostbyname(url.hostname)),
                },
                "REMOTE_TLS",
            ),
        ]:
            client = Client(bad, "controller")
            try:
                try:
                    client.probe()
                except DispatcherError as error:
                    assert error.code == code
                else:
                    raise AssertionError("Invalid TLS/auth accepted")
            finally:
                client.close()
        ok("tls_hostname_certificate_auth")
        proof["tls"] = dict(
            url=config["relay_url"],
            verified=True,
            rejected=["bad_credential", "untrusted_CA", "wrong_hostname"],
        )
        offer = output / "offer-private.json"
        public = call(
            "remote_pairing_export",
            operator_file=str(output / "operator-private.json"),
            url=config["relay_url"],
            destination=str(offer),
        )
        registration = peer.call(
            "pair", offer=json.loads(offer.read_bytes()), controller=public["controller"]
        )
        agent_id = registration["fingerprint"]
        safe_json(output / "registration.json", registration["registration"])
        try:
            call(
                "remote_pairing_finish", registration=str(output / "registration.json"), fingerprint="0" * 64
            )
        except DispatcherError as error:
            assert error.code == "REMOTE_FINGERPRINT"
        else:
            raise AssertionError("Wrong fingerprint accepted")
        call("remote_pairing_finish", registration=str(output / "registration.json"), fingerprint=agent_id)
        assert call("remote_agents")[0]["state"] == "PENDING" and not online()
        call("remote_approve", id=agent_id, fingerprint=agent_id)
        assert not online() and call("remote_diagnose", id=agent_id)["relay"]
        peer.call("start", role="agent")
        wait(online)
        observation = wait(lambda: peer.call("observe"))
        assert observation["platform"] == "linux" and observation["uid"] != 0
        assert observation["program_commit"] == info["commit"]
        assert (
            observation["helper_sha256"]
            == hashlib.sha256((Path(__file__).parent / "crosshost/linux_peer.py").read_bytes()).hexdigest()
        )
        ok("linux_processes", "pairing_fingerprints_approval", "signed_heartbeat")
        proof["linux"] = {
            k: observation[k] for k in ("os", "packages", "uid", "program_commit", "helper_sha256", "roles")
        }
        window.capture_as_image().save(output / "windows-controller.png")
        fixture_url = observation["url"]
        if call("remote_agents")[0]["version"] == "5D.1":
            phase = "Stage 5D scoped browser session on frozen Windows to real Linux"
            from poster.maker.profiles import Profiles

            profile_key = Fernet.generate_key()
            profile_key_file = output / "maker-profile-private.key"
            profile_key_file.write_bytes(profile_key)
            profiles = Profiles(output / "private-profiles", key=profile_key)
            profile_id = profiles.create("Synthetic scoped session")
            session_origin = "http://fixture.poster.test"
            # A controlled virtual origin is reachable only through the actual
            # authenticated HTTP proxy on Linux, never through public DNS.
            fixture_address = urlsplit(fixture_url)
            call(
                "network_bind",
                binding="crosshost-network",
                config=dict(
                    kind="http",
                    host=fixture_address.hostname,
                    port=fixture_address.port,
                    username="poster",
                    password="POSTER_5D_PROXY_PASSWORD",
                ),
            )
            profiles.open(profile_id).commit(
                dict(
                    cookies=[
                        dict(
                            name="poster5d",
                            value="POSTER_5D_SYNTHETIC_SESSION",
                            domain="fixture.poster.test",
                            path="/",
                            httpOnly=True,
                            sameSite="Lax",
                        )
                    ],
                    origins=[
                        dict(
                            origin=session_origin,
                            localStorage=[dict(name="poster5d", value="POSTER_5D_SYNTHETIC_STORAGE")],
                        )
                    ],
                )
            )
            session_graph = chain(
                "Cross-host Stage 5D session",
                [
                    ("CSV.Read", dict(path="inputs/data.csv")),
                    ("Files.Copy", dict(source="inputs/data.csv", destination="resource-proof.csv")),
                    ("Browser.OpenURL", dict(url=session_origin + "/session-check")),
                    ("Browser.ReadText", dict(selector="#session-authorized")),
                    ("Files.WriteText", dict(path="session-proof.txt", text="{{authentication}}")),
                ],
            )
            extraction = next(n for n in session_graph.nodes if n.type == "Browser.ReadText")
            extraction.save_as, extraction.result_path = "authentication", "value"
            resource = b"name,value\nportable,5D\n"
            private_dir(output / "inputs")
            (output / "inputs/data.csv").write_bytes(resource)
            session_graph.metadata.update(
                {
                    "poster.resources": {"inputs/data.csv": hashlib.sha256(resource).hexdigest()},
                    "poster.network": dict(binding="crosshost-network", allow_override=False),
                }
            )
            session_source = output / "session-project.postergraph.json"
            save_graph(session_source, session_graph, Vault())
            session_item = call("add", source=str(session_source))
            permit_args = dict(
                agent_id=agent_id,
                project_id=session_item["project_id"],
                version=session_item["version"],
                profile_root=str(profiles.root),
                profile_id=profile_id,
                key_file=str(profile_key_file),
                origins=[session_origin],
                confirmed=True,
            )
            permit = call("session_permission_create", **permit_args)
            queued = call(
                "enqueue_batch",
                items=[
                    dict(
                        id=session_item["id"],
                        agent_id=agent_id,
                        session_permission=permit["id"],
                        remote_outputs=["session-proof.txt", "resource-proof.csv"],
                    )
                ],
                request_id=uuid.uuid4().hex,
            )
            assert not queued["errors"]
            completed = terminal(queued["jobs"][0])
            assert completed["status"] == "SUCCEEDED"
            returned = Path(completed["workdir"]) / "session-proof.txt"
            assert returned.read_text() == "Session authorized"
            assert (Path(completed["workdir"]) / "resource-proof.csv").read_bytes() == resource
            permit_revocation = call("session_permission_revoke", id=permit["id"])
            assert permit_revocation["may_have_been_disclosed"]
            unsent = call("session_permission_create", **permit_args)
            revoked = call("session_permission_revoke", id=unsent["id"])
            assert revoked["revoked_before_transfer"]
            refusal = call(
                "enqueue_batch",
                items=[dict(id=session_item["id"], agent_id=agent_id, session_permission=unsent["id"])],
                request_id=uuid.uuid4().hex,
            )
            assert not refusal["jobs"] and refusal["errors"][0]["code"] == "PROFILE_PERMISSION"
            proof["stage5d_session"] = dict(
                status="PASSED",
                agent_id=agent_id,
                job=queued["jobs"][0],
                linux_browser_pid=completed["history"][0]["pid"],
                returned_sha256=hashlib.sha256(returned.read_bytes()).hexdigest(),
                cookies_and_storage_confirmed=True,
                proxy_auth_verified=True,
                pinned_resource_sha256=hashlib.sha256(resource).hexdigest(),
                revoked_before_transfer=True,
                queue_key_transferred=False,
            )
        phase = "snapshot, protected types, parallel limits"
        secret = dict(n=17, ok=True, nested=[None, 42, False], token=canary)
        first, item, source, original = enqueue(
            [
                ("Variables.Compare", {"left": {"variable": "typed_count"}, "right": 17}),
                (
                    "HTTP.Post",
                    {"url": fixture_url + "/first", "body_type": "json", "body": {"variable": "protected"}},
                ),
                ("Files.WriteText", {"path": "out.txt", "text": "cross-host result"}),
            ],
            variables={
                "protected": Variable(input=True, secret=True, required=True),
                "typed_count": Variable(value=0, input=True, required=True),
            },
            inputs={"typed_count": 17},
            secrets={"protected": secret},
            outputs=["out.txt"],
        )
        wait(lambda: peer.call("observe")["calls"])
        run = call("show", id=first)["history"][0]["run_id"]
        master = (root / "queue.key").read_bytes().strip()
        with sqlite3.connect(root / "dispatcher.sqlite") as db:
            encrypted = db.execute("SELECT key_cipher FROM remote_runs WHERE run_id=?", (run,)).fetchone()[0]
        per_run = json.loads(Fernet(master).decrypt(encrypted))["key"].encode()
        evidence = peer.call("bundle", run=run, version=item["version"])
        files = json.loads(Fernet(per_run).decrypt(base64.b64decode(evidence["bundle"])))["files"]
        assert base64.b64decode(files[source.name]) == original
        calls = peer.call("observe")["calls"]
        assert calls[0]["sha256"] == hashlib.sha256(json.dumps(secret, sort_keys=True).encode()).hexdigest()
        assert calls[0]["types"]["n"] == "int" and calls[0]["types"]["ok"] == "bool"
        ok("exact_maker_snapshot", "typed_and_protected_inputs")
        proof["snapshot"] = dict(
            version=item["version"],
            sha256=hashlib.sha256(original).hexdigest(),
            original_bytes_equal=True,
            run_id=run,
        )
        # Same project must remain pending while max_per_project=1.
        queued = call(
            "enqueue_batch",
            items=[
                dict(
                    id=item["id"],
                    agent_id=agent_id,
                    secrets={"protected": secret},
                    inputs={"typed_count": 17},
                    remote_outputs=["out.txt"],
                )
            ],
            request_id=uuid.uuid4().hex,
        )["jobs"][0]
        second, *_ = enqueue([("HTTP.Post", {"url": fixture_url + "/second"})])
        wait(lambda: len(peer.call("observe")["calls"]) == 2)
        active = call("jobs", status="RUNNING")["rows"]
        assert len(active) == 2 and len({r["pid"] for r in active}) == 2
        assert call("show", id=queued)["status"] == "PENDING"
        third, *_ = enqueue(
            [("Files.WriteText", {"path": "third.txt", "text": "third"})], outputs=["third.txt"]
        )
        time.sleep(1)
        assert call("show", id=third)["status"] == "PENDING"
        proof["parallel"] = dict(
            active=[{k: row.get(k) for k in ("id", "pid", "birth", "status")} for row in active],
            observed_linux=peer.call("observe")["processes"],
            same_project_pending=True,
            global_limit_pending=True,
        )
        ok("two_parallel_executions", "global_and_project_limits")
        peer.call("release", label="first")
        peer.call("release", label="second")
        for job in (first, second, queued, third):
            value = terminal(job)
            assert value["status"] == "SUCCEEDED" and len(value["history"]) == 1
        value = call("show", id=first)
        assert (Path(value["workdir"]) / "out.txt").read_text() == "cross-host result"
        assert value["report"]["remote_agent"] == agent_id and value["report"]["gui_imports_forbidden"]
        returned = output / "returned-files"
        returned.mkdir()
        for job, filename, expected in [
            (first, "out.txt", b"cross-host result"),
            (third, "third.txt", b"third"),
        ]:
            value = call("show", id=job)
            raw = (Path(value["workdir"]) / filename).read_bytes()
            assert raw == expected
            (returned / filename).write_bytes(raw)
            proof["returned_files"].append(
                dict(name=filename, sha256=hashlib.sha256(raw).hexdigest(), bytes=len(raw))
            )
        ok("linux_graphrunner_output", "windows_history")
        phase = "cancel and real browser timeout"
        cancelled, *_ = enqueue([("Logic.Delay", {"milliseconds": 120000})])
        wait(lambda: call("show", id=cancelled)["status"] == "RUNNING")
        call("cancel", ids=[cancelled])
        assert terminal(cancelled)["status"] in {"CANCELLED", "UNKNOWN"}
        ok("cancellation")
        browser, *_ = enqueue(
            [("Browser.OpenURL", {"url": fixture_url}), ("Logic.Delay", {"milliseconds": 120000})], timeout=8
        )
        seen_browser = wait(
            lambda: [p for p in peer.call("observe")["processes"]["alive"] if "chrom" in p["name"].lower()]
        )
        assert seen_browser
        proof["browser"] = dict(observed=seen_browser)
        assert terminal(browser)["status"] == "UNKNOWN"
        wait(lambda: not peer.call("observe")["processes"]["alive"])
        proof["browser"]["alive_after_timeout"] = []
        ok("browser_timeout_cleanup", "no_orphan_browsers")
        phase = "stop and reconnect"
        before = peer.call("observe")["identity_sha256"]
        peer.call("stop", role="agent")
        wait(lambda: not online())
        assert "REMOTE_STOPPED" in call("remote_diagnose", id=agent_id)["codes"]
        peer.call("start", role="agent")
        wait(online)
        assert peer.call("observe")["identity_sha256"] == before
        proof["reconnect"] = dict(identity_preserved=True, roles=peer.call("observe")["roles"])
        ok("agent_disconnect_reconnect")
        phase = "partition after POST; UNKNOWN never retries"
        lost, *_ = enqueue([("HTTP.Post", {"url": fixture_url + "/lost"})], timeout=90)
        wait(lambda: any(c["label"] == "lost" for c in peer.call("observe")["calls"]))
        peer.call("kill", role="relay")
        assert terminal(lost)["status"] == "UNKNOWN"
        peer.call("release", label="lost")
        peer.call("start", role="relay")
        wait(online)
        time.sleep(3)
        assert len([c for c in peer.call("observe")["calls"] if c["label"] == "lost"]) == 1
        assert call("show", id=lost)["status"] == "UNKNOWN" and len(call("show", id=lost)["history"]) == 1
        proof["partition"] = dict(job_id=lost, status="UNKNOWN", post_count=1, history_count=1)
        ok("post_partition_unknown_no_resend")
        phase = "private boundary and revocation"
        wait(lambda: not peer.call("observe")["processes"]["alive"])
        remote_files = peer.call("scan")["files"]
        assert all(
            master not in base64.b64decode(raw)
            and all(
                secret not in base64.b64decode(raw)
                for secret in (
                    canary.encode(),
                    b"POSTER_5D_SYNTHETIC_SESSION",
                    b"POSTER_5D_SYNTHETIC_STORAGE",
                    b"POSTER_5D_PROXY_PASSWORD",
                )
            )
            for raw in remote_files.values()
        )
        public = list(root.rglob("report.json")) + [
            root / "dispatcher.sqlite",
            output / "observer-private.json",
        ]
        for path in public:
            if path.exists():
                assert canary.encode() not in path.read_bytes()
        ok("secret_and_queue_key_boundary")
        proof["secret_boundary"] = dict(
            queue_key_transferred=False,
            linux_files_checked=len(remote_files),
            plaintext_canary_absent=True,
        )
        call("remote_revoke", id=agent_id)
        assert not online()
        count_before_revoke_check = call("jobs")["total"]
        refused = call(
            "enqueue_batch",
            items=[
                dict(
                    id=item["id"],
                    agent_id=agent_id,
                    secrets={"protected": secret},
                    inputs={"typed_count": 17},
                    remote_outputs=["out.txt"],
                )
            ],
            request_id=uuid.uuid4().hex,
        )
        assert not refused["jobs"] and refused["errors"][0]["code"] == "REMOTE_REVOKED"
        assert call("jobs")["total"] == count_before_revoke_check
        proof["revoke"] = dict(error="REMOTE_REVOKED", admitted_jobs=0, queue_count_unchanged=True)
        ok("revoke_without_fallback")
        proof["fixture"] = peer.call("observe")["calls"]
        # Preserve the complete database only in private output. Publish an
        # explicit safe column allowlist; inputs and key/profile ciphers stay local.
        proof.update(export_history(root / "dispatcher.sqlite", output))
        assert set(passed) == set(SCENARIOS)
        return report(
            output / "report.json",
            status="PASSED",
            scenarios=passed,
            commit=info["commit"],
            windows_frozen=True,
            linux_non_root=True,
            separate_hosts=True,
            real_tcp_tls=True,
            proof=proof,
        )
    except Exception as exc:
        if peer.last_failure:
            proof["peer_failure"] = peer.last_failure
        if (root / "dispatcher.sqlite").exists() and not (output / "windows-history-safe.sqlite").exists():
            try:
                proof.update(export_history(root / "dispatcher.sqlite", output))
            except Exception as history_error:
                proof["history_export_error"] = type(history_error).__name__
        report(
            output / "report.json",
            status="FAILED",
            scenarios=passed,
            phase=phase,
            error_type=type(exc).__name__,
            failure_line=next(
                (
                    frame.lineno
                    for frame in reversed(traceback.extract_tb(exc.__traceback__))
                    if Path(frame.filename).resolve() == Path(__file__).resolve()
                ),
                None,
            ),
            proof=proof,
        )
        raise RuntimeError("Cross-host failed at " + phase) from None
    finally:
        if prepared:
            try:
                peer.call("cleanup")
            except Exception:
                pass
        try:
            call("shutdown")
        except Exception:
            pass
        for process in processes:
            if process.poll() is None:
                process.terminate()
                process.wait(30)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--config", type=Path, help="Private local Windows/SSH configuration; never upload")
    group.add_argument("--not-executed", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.not_executed:
        value = report(
            args.output / "report.json",
            status="NOT_EXECUTED",
            reason="No simultaneously connected trusted Windows frozen Controller and Linux Agent infrastructure",
        )
    else:
        value = check(json.loads(args.config.read_bytes()), args.output.resolve())
    print(json.dumps(value))


if __name__ == "__main__":
    main()
