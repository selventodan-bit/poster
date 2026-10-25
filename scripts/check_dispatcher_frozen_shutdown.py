"""Early frozen headless lifecycle regression, in addition to the full GUI E2E."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import time

from poster.dispatcher.host import request
from poster.dispatcher.launcher import environment, process_options
from poster.dispatcher.models import DispatcherError
from poster.maker.example_projects import chain
from poster.maker.storage import save_graph, Vault


def check(bundle, output):
    output.mkdir(parents=True, exist_ok=False)
    windows = Path(os.environ["SystemRoot"])
    env = environment()
    env["PATH"] = os.pathsep.join(map(str, [windows / "System32", windows]))
    info = json.loads((bundle / "BUILD_INFO.json").read_bytes())
    assert info["commit"] == os.environ["EXPECTED_POSTER_COMMIT"]
    worker = bundle / "PosterWorker.exe"
    disabled = bundle / "PosterWorker.exe.disabled"
    results = []
    for case in ["idle", "unavailable", "completed"]:
        root = output / case
        process = None
        try:
            if case == "unavailable":
                worker.rename(disabled)
            process = subprocess.Popen(
                [str(bundle / "PosterService.exe"), "--workspace", str(root)],
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                **process_options(),
            )
            deadline = time.monotonic() + 30
            while True:
                try:
                    state = request(root, "ping")
                    break
                except DispatcherError:
                    if process.poll() is not None or time.monotonic() > deadline:
                        raise AssertionError("Service startup did not complete") from None
                    time.sleep(0.05)
            assert state["pid"] == process.pid
            if case == "unavailable":
                try:
                    request(root, "start")
                except DispatcherError as exc:
                    assert exc.code == "WORKER_UNAVAILABLE"
                else:
                    raise AssertionError("Missing worker accepted")
            elif case == "completed":
                graph_path = output / "sample.postergraph.json"
                save_graph(
                    graph_path,
                    chain(
                        "Frozen service data",
                        [("Files.WriteText", {"path": "data.txt", "text": "frozen data"})],
                    ),
                    Vault(),
                )
                item = request(root, "add", source=str(graph_path))
                job = request(root, "enqueue", ids=[item["id"]], request_id="once")[0]
                request(root, "start")
                deadline = time.monotonic() + 30
                while request(root, "show", id=job)["status"] in {"PENDING", "LEASED", "RUNNING"}:
                    if time.monotonic() > deadline:
                        raise AssertionError("Data worker deadline")
                    time.sleep(0.05)
                value = request(root, "show", id=job)
                assert value["status"] == "SUCCEEDED"
                assert value["report"]["worker_frozen"] and value["report"]["browser_imports_forbidden"]
                assert (Path(value["workdir"]) / "data.txt").read_text() == "frozen data"
                request(root, "stop")
            assert request(root, "shutdown")
            assert process.wait(timeout=15) == 0
            trace = json.loads((root / "service.json").read_bytes())
            results.append({"case": case, "pid": process.pid, "exit": 0, "trace": trace})
        except Exception as exc:
            trace = (
                json.loads((root / "service.json").read_bytes()) if (root / "service.json").exists() else {}
            )
            print(
                "\n::error title=Frozen service lifecycle::"
                + json.dumps({"case": case, "type": type(exc).__name__, "trace": trace}),
                flush=True,
            )
            raise AssertionError("Frozen service lifecycle: " + case) from None
        finally:
            if process and process.poll() is None:
                process.kill()
                process.wait(5)
            if disabled.exists():
                disabled.rename(worker)
    report = {"commit": info["commit"], "status": "SUCCEEDED", "cases": results}
    (output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print("\n::notice title=Frozen service lifecycle::" + json.dumps(report), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    check(args.bundle.resolve(), args.output.resolve())
