import json
from pathlib import Path
import subprocess
import sys

from poster.dispatcher.store import Store
from poster.maker.example_projects import EXAMPLES, source_directory, chain
from poster.maker.fixtures import serve_maker
from poster.maker.model import Variable
from poster.maker.storage import Vault, save_graph


def cli(directory, *args, expected=0):
    process = subprocess.run(
        [sys.executable, "-m", "poster", "dispatcher", *map(str, args)],
        cwd=directory,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert process.returncode == expected, (process.stdout, process.stderr)
    return json.loads(process.stdout)


def test_public_cli_executes_all_four_real_examples_and_preserves_history(tmp_path):
    db = tmp_path / "очередь с пробелами.sqlite"
    with serve_maker() as (_, base):
        jobs = []
        for i, (_, name) in enumerate(EXAMPLES):
            options = ["--input", "api_base=" + base] if i == 0 else []
            job = cli(tmp_path, "enqueue", source_directory() / name, "--db", db, "--key", str(i), *options)
            jobs.append(job["id"])
        result = cli(tmp_path, "run", "--db", db, "--once", "--max-workers", 2)
        assert result["counts"] == {"SUCCEEDED": 4}
        assert cli(tmp_path, "list", "--db", db)["counts"] == {"SUCCEEDED": 4}
        for job in jobs:
            detail = cli(tmp_path, "show", job, "--db", db, "--report")
            assert detail["status"] == "SUCCEEDED" and detail["report"]["browser_imports_forbidden"]
            assert len(cli(tmp_path, "history", job, "--db", db)["executions"]) == 1
        second = cli(tmp_path, "show", jobs[1], "--db", db, "--report")["report"]
        assert json.loads((Path(second["workdir"]) / "lists/result.json").read_text()) == [
            "ALPHA",
            "BETA",
            "ГАММА",
        ]
        third = cli(tmp_path, "show", jobs[2], "--db", db, "--report")["report"]
        assert (Path(third["workdir"]) / "tables/output.xlsx").is_file()


def test_cli_secrets_are_encrypted_and_invalid_inputs_are_payload_free(tmp_path):
    secret = "CLI_SECRET_CANARY_29853"
    graph = chain(
        "secret",
        [
            (
                "Variables.Compare",
                {"left": "{{password}}", "right": {"token": secret, "flag": True, "number": 7}},
            )
        ],
    )
    graph.variables["password"] = Variable(value=None, input=True, required=True, secret=True)
    source = tmp_path / "source" / "main.postergraph.json"
    source.parent.mkdir()
    save_graph(source, graph, Vault())
    incoming = tmp_path / "private-input.json"
    incoming.write_text(json.dumps({"password": {"token": secret, "flag": True, "number": 7}}))
    db = tmp_path / "q.sqlite"
    queued = cli(tmp_path, "enqueue", source, "--db", db, "--secrets-file", incoming)
    assert cli(tmp_path, "run", "--db", db, "--once")["counts"] == {"SUCCEEDED": 1}
    shown = cli(tmp_path, "show", queued["id"], "--db", db, "--report")
    assert secret not in json.dumps(shown)
    raw = b"".join(p.read_bytes() for p in tmp_path.glob("q.sqlite*") if p.is_file())
    raw += b"".join(p.read_bytes() for p in (tmp_path / "q.sqlite.data").rglob("*") if p.is_file())
    assert secret.encode() not in raw
    incoming.write_text('{"' + secret + '":')
    failed = cli(tmp_path, "enqueue", source, "--db", db, "--secrets-file", incoming, expected=1)
    assert failed["code"] == "INPUT_INVALID" and secret not in json.dumps(failed)


def test_cli_cancel_stop_reconcile_and_idempotent_enqueue(tmp_path):
    db = tmp_path / "q.sqlite"
    source = source_directory() / EXAMPLES[1][1]
    first = cli(tmp_path, "enqueue", source, "--db", db, "--key", "logical")
    assert cli(tmp_path, "enqueue", source, "--db", db, "--key", "logical")["id"] == first["id"]
    assert cli(tmp_path, "cancel", first["id"], "--db", db)["status"] == "CANCELLED"
    assert cli(tmp_path, "stop", "--db", db)["status"] == "STOP_REQUESTED"
    assert cli(tmp_path, "reconcile", "--db", db)["executed"] == 0
    assert cli(tmp_path, "run", "--db", db, "--once")["counts"] == {"CANCELLED": 1}
    with_key = Store(db)
    assert with_key.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    with_key.close()
