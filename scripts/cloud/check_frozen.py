"""Real new frozen Maker -> HTTP Drive fixture -> Jupyter -> frozen Dispatcher.

External driver has Python. Portable Windows applications get system-only PATH;
all interactions are real OS input, observed geometry is read-only/test-only.
"""

import hashlib
import json
import os
from pathlib import Path
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "tests")]


class StableDispatcherControl:
    """Two fresh observer generations after focus, for the expected frozen PID.

    completed(connect) observes before Qt handles the banner's LayoutRequest.
    Its first valid/connected frame can therefore contain obsolete coordinates.
    """

    def __init__(self, name, pid):
        self.name, self.pid = name, pid
        self.generation = self.point = None
        self.count = 0
        self.frames = []

    def observe(self, state, generation):
        widget = state.get("widgets", {}).get(self.name)
        if (
            state.get("pid") != self.pid
            or not state.get("connected")
            or state.get("busy")
            or not widget
            or not widget.get("enabled")
        ):
            self.point, self.count = None, 0
            return None
        point = (widget["x"], widget["y"])
        if generation != self.generation:
            self.count = self.count + 1 if point == self.point else 1
            self.point, self.generation = point, generation
            self.frames.append(dict(x=point[0], y=point[1]))
            self.frames = self.frames[-8:]
        return widget if self.count >= 2 else None


def main():
    if os.name != "nt":
        raise RuntimeError("REAL_WINDOWS_REQUIRED")
    import psutil
    import win32clipboard
    import win32con
    from pywinauto import Desktop, keyboard, mouse
    from windows_gui_launch import open_portable
    from cloud_drive_fixture import serve_drive_fixture
    from poster.dispatcher.host import request
    from poster.maker.example_projects import chain
    from poster.maker.storage import Vault, save_graph
    import nbformat
    from nbclient import NotebookClient
    from jupyter_client import KernelManager

    bundle = Path(os.environ["POSTER_CLOUD_FROZEN_BUNDLE"]).resolve()
    info = json.loads((bundle / "BUILD_INFO.json").read_bytes())
    assert info["commit"] == os.environ["GITHUB_SHA"]
    private = ROOT / ".poster/cloud-frozen-private"
    private.mkdir(parents=True, exist_ok=False)
    public = ROOT / ".poster/cloud-frozen-public"
    public.mkdir(parents=True, exist_ok=False)
    observer = private / "cloud-observer.json"
    maker_observer = private / "maker-observer.json"
    dispatcher_observer = private / "dispatcher-observer.json"
    queue = private / "Dispatcher Ж"
    os.environ.update(
        POSTER_CLOUD_TEST_OBSERVER=str(observer),
        POSTER_GUI_TEST_OUTPUT=str(maker_observer),
        POSTER_DISPATCHER_OBSERVER=str(dispatcher_observer),
        POSTER_DISPATCHER_WORKSPACE=str(queue),
    )
    owned = []
    phase = "prepare"
    proof = dict(
        status="FAILED",
        commit=info["commit"],
        windows=os.name,
        provider="CONTROLLED_HTTP_DRIVE_FIXTURE",
        live_provider="CLOUD_PROVIDER_NOT_EXECUTED",
        processes=[],
        scenarios=[],
    )

    def snapshot(path):
        try:
            return json.loads(path.read_bytes())
        except (OSError, ValueError):
            return {}

    def wait(predicate, label, timeout=75):
        nonlocal phase
        phase = label
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            value = predicate()
            if value:
                return value
            time.sleep(0.08)
        raise RuntimeError("FROZEN_CLOUD_DEADLINE")

    def click(path, name, window):
        # Require stable geometry across two observer generations.
        previous = None
        for _ in range(100):
            state = snapshot(path)
            point = state.get("positions", {}).get(name)
            if point and not state.get("busy"):
                if point == previous:
                    window.set_focus()
                    mouse.click(coords=tuple(point))
                    return
                previous = point
            time.sleep(0.12)
        raise RuntimeError("FROZEN_CLOUD_CONTROL_UNAVAILABLE")

    def paste(value):
        win32clipboard.OpenClipboard()
        try:
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardText(value, win32con.CF_UNICODETEXT)
        finally:
            win32clipboard.CloseClipboard()
        keyboard.send_keys("^v", pause=0.05)

    def record(window, name):
        p = psutil.Process(window.process_id())
        owned.append(p)
        assert Path(p.exe()).resolve() == bundle / name
        proof["processes"].append(dict(pid=p.pid, birth=p.create_time(), executable=name))

    def dispatcher_click(name):
        dispatcher.set_focus()  # Activation/DPI/layout changes precede observation.
        stable = StableDispatcherControl(name, dispatcher.process_id())

        def ready():
            state = snapshot(dispatcher_observer)
            try:
                generation = dispatcher_observer.stat().st_mtime_ns
            except OSError:
                return None
            return stable.observe(state, generation)

        state = wait(ready, "Dispatcher stable control: " + name)
        proof.setdefault("dispatcher_controls", []).append(
            dict(name=name, pid=stable.pid, frames=stable.frames)
        )
        mouse.click(coords=(state["x"], state["y"]))  # Real OS input, no internal callback.

    try:
        with serve_drive_fixture() as (api, url):
            config = private / "fixture.json"
            config.write_text(json.dumps(dict(mode="drive_fixture", nonce=uuid.uuid4().hex, url=url)))
            os.environ["POSTER_CLOUD_TEST_CONFIG"] = str(config)
            source = private / "project.postergraph.json"
            resource = b"n\n42\n"
            (private / "data.csv").write_bytes(resource)
            graph = chain(
                "Frozen Drive Ж",
                [
                    ("CSV.Read", {"path": "data.csv"}),
                    ("Files.WriteText", {"path": "out.txt", "text": "frozen notebook result"}),
                ],
            )
            graph.metadata.update(
                {
                    "poster.resources": {"data.csv": hashlib.sha256(resource).hexdigest()},
                    "poster.outputs": ["out.txt"],
                }
            )
            save_graph(source, graph, Vault())
            open_portable(bundle / "Poster.exe")
            maker = Desktop(backend="win32").window(title="Poster 3.0 — ProjectMaker")
            maker.wait("visible", timeout=60)
            record(maker, "Poster.exe")
            wait(lambda: snapshot(maker_observer).get("ui") == "qt", "Maker ready")
            click(maker_observer, "open", maker)
            dialog = Desktop(backend="win32").window(title="Открыть граф")
            dialog.wait("visible", timeout=20)
            dialog.set_focus()
            keyboard.send_keys("%n")
            paste(str(source))
            keyboard.send_keys("{ENTER}")
            wait(
                lambda: snapshot(maker_observer).get("graph", {}).get("id") == graph.id, "Maker opened graph"
            )
            click(maker_observer, "cloud_save", maker)
            cloud = Desktop(backend="win32").window(title="Google Drive — проекты Poster")
            cloud.wait("visible", timeout=30)
            wait(
                lambda: snapshot(observer).get("visible") and not snapshot(observer).get("busy"),
                "Cloud ready",
            )
            click(observer, "cloud_connect", cloud)
            wait(
                lambda: (
                    len(snapshot(observer).get("projects", [])) == 1 and not snapshot(observer).get("busy")
                ),
                "Frozen Maker published",
            )
            selected = snapshot(observer)["projects"][0]
            proof["scenarios"].append("frozen_maker_published_immutable_resources")
            cloud.capture_as_image().save(public / "maker-drive.png")
            click(observer, "cloud_close", cloud)
            maker.close()
            wait(lambda: not maker.exists(), "Maker closed")
            # Genuine Jupyter kernel, not an in-process fake notebook frontend.
            notebook = nbformat.v4.new_notebook(
                cells=[
                    nbformat.v4.new_code_cell(f"""
from pathlib import Path
import json
from poster.cloud.desktop import DesktopCloud
from poster.cloud.providers import ProviderConnection
from poster.cloud.launcher import Launcher
facade=DesktopCloud()
await facade.connect()
backend=Launcher('generic',connection=ProviderConnection(await facade._library(),facade),root=Path({str(private / "notebook")!r}))
await backend.connect()
result=await backend.run({selected["project_id"]!r},{selected["version"]!r})
assert result['runs'][0]['status']=='SUCCEEDED',result
Path({str(private / "notebook-proof.json")!r}).write_text(json.dumps(result))
await backend.close()
""")
                ]
            )
            manager = KernelManager(kernel_name="python3")
            manager.kernel_spec.argv[0] = sys.executable
            NotebookClient(
                notebook, km=manager, timeout=90, resources={"metadata": {"path": str(ROOT)}}
            ).execute()
            native = json.loads((private / "notebook-proof.json").read_bytes())
            assert native["results_committed"]
            proof["notebook"] = native
            proof["scenarios"].append("genuine_jupyter_original_graphrunner_committed_results")
            open_portable(bundle / "PosterDispatcher.exe")
            dispatcher = Desktop(backend="win32").window(title="Poster Dispatcher — очередь проектов")
            dispatcher.wait("visible", timeout=60)
            record(dispatcher, "PosterDispatcher.exe")
            wait(lambda: snapshot(dispatcher_observer).get("connected"), "Dispatcher ready")
            dispatcher_click("cloud_projects")
            phase = "Dispatcher Drive window after OS click"
            cloud.wait("visible", timeout=30)
            wait(
                lambda: (
                    len(snapshot(observer).get("projects", [])) == 1 and not snapshot(observer).get("busy")
                ),
                "Frozen Dispatcher catalog",
            )
            click(observer, "cloud_open", cloud)
            wait(lambda: len(request(queue, "projects")) == 1, "Frozen Dispatcher imported pinned project")
            rows = request(queue, "projects")
            assert rows[0]["cloud"]["version"] == selected["version"]
            # Reopen and use the real frozen UI's result importer.
            dispatcher_click("cloud_projects")
            phase = "Dispatcher Drive reopen after OS click"
            cloud.wait("visible", timeout=30)
            wait(lambda: not snapshot(observer).get("busy"), "Results ready")
            click(observer, "cloud_results", cloud)
            results = wait(lambda: request(queue, "cloud_results"), "Frozen Dispatcher result history")
            assert (
                len(results) == 1 and results[0]["mode"] == "DETACHED" and results[0]["status"] == "SUCCEEDED"
            )
            assert (Path(results[0]["directory"]) / "out.txt").read_text() == "frozen notebook result"
            assert request(queue, "jobs")["total"] == 0
            result_window = Desktop(backend="win32").window(title="Автономные результаты — DETACHED")
            result_window.wait("visible", timeout=20)
            result_window.capture_as_image().save(public / "dispatcher-results.png")
            result_window.set_focus()
            keyboard.send_keys("{ESC}")
            click(observer, "cloud_close", cloud)
            dispatcher.close()
            request(queue, "shutdown")
            proof["results"] = [
                {k: r[k] for k in ("mode", "run_id", "project_version", "status", "evidence", "outputs")}
                for r in results
            ]
            proof["scenarios"].extend(
                [
                    "frozen_dispatcher_pinned_cloud_catalog",
                    "frozen_dispatcher_imported_offline_encrypted_files",
                    "managed_history_untouched",
                ]
            )
            assert all(b"POSTER_SESSION_KEY" not in f.get("raw", b"") for f in api.files.values())
            proof["http_fixture_requests"] = len(api.requests)
            proof["status"] = "PASSED"
    except Exception as exc:
        proof.update(phase=phase, error_type=type(exc).__name__)
        # Payload-free state only. Never publish the complete observer, account
        # settings, widget text, inputs or a private workspace on failure.
        state = snapshot(dispatcher_observer)
        control = state.get("widgets", {}).get("cloud_projects")
        proof["dispatcher_failure"] = {
            k: state.get(k) for k in ("pid", "connected", "busy", "error", "action")
        }
        proof["dispatcher_failure"]["cloud_projects"] = control
        if "dispatcher" in locals():
            try:
                dispatcher.capture_as_image().save(public / "dispatcher-failure.png")
            except Exception:
                pass  # Diagnostics must not replace the original failure.
        raise
    finally:
        try:
            request(queue, "shutdown")
        except Exception:
            pass
        for process in reversed(owned):
            try:
                for child in process.children(recursive=True):
                    child.kill()
                process.kill()
            except psutil.NoSuchProcess:
                pass
        (public / "proof.json").write_text(json.dumps(proof, indent=2))
        print("::notice title=Frozen Stage 5C E2E::" + json.dumps(proof))


if __name__ == "__main__":
    main()
