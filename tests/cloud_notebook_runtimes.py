"""Genuine installed Jupyter kernel and marimo runtime, real HTTP Drive fixture."""

import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

import nbformat
from nbclient import NotebookClient
from jupyter_client import KernelManager
import pytest

from cloud_drive_fixture import serve_drive_fixture
from poster.cloud.desktop import DesktopCloud
from poster.maker.example_projects import chain
from poster.maker.storage import Vault, save_graph


@pytest.mark.parametrize("kind", ["jupyter", "marimo"])
async def test_installed_notebook_runtime_executes_original_workers_and_returns_results(
    tmp_path, monkeypatch, kind
):
    with serve_drive_fixture() as (_, url):
        config = tmp_path / "fixture.json"
        config.write_text(json.dumps(dict(mode="drive_fixture", nonce=uuid.uuid4().hex, url=url)))
        monkeypatch.setenv("POSTER_CLOUD_TEST_CONFIG", str(config))
        facade = DesktopCloud()
        source = tmp_path / "project.postergraph.json"
        save_graph(
            source,
            chain(
                "Genuine runtime",
                [
                    ("CSV.Read", {"path": "data.csv"}),
                    ("Files.WriteText", {"path": "out.txt", "text": "native GraphRunner"}),
                ],
            ),
            Vault(),
        )
        (tmp_path / "data.csv").write_text("n\n42\n")
        await facade.connect()
        published = await facade.publish(source, resources=["data.csv"], outputs=["out.txt"])
        proof = tmp_path / (kind + "-proof.json")
        code = f"""
import json
from pathlib import Path
from poster.cloud.desktop import DesktopCloud
from poster.cloud.providers import ProviderConnection
from poster.cloud.launcher import Launcher, JupyterLauncher
from poster.cloud.marimo_frontend import MarimoLauncher
facade = DesktopCloud()
await facade.connect()
connection = ProviderConnection(await facade._library(), facade)
backend = Launcher('generic', connection=connection, root=Path({str(tmp_path / "runtime")!r}))
rows = await backend.connect()
assert rows[0]['project_id'] == {published["project_id"]!r}
versions = await backend.versions(rows[0]['project_id'])
assert versions[-1]['version'] == {published["version"]!r}
result = await backend.run(rows[0]['project_id'],versions[-1]['version'])
assert result['runs'][0]['status'] == 'SUCCEEDED', result
assert result['runs'][0]['pid'] is not None
Path({str(proof)!r}).write_text(json.dumps(result))
await backend.close()
"""
        if kind == "jupyter":
            notebook = nbformat.v4.new_notebook(
                cells=[
                    nbformat.v4.new_code_cell(
                        code + "\nui = JupyterLauncher(Launcher()); display(ui.widget)\n"
                    )
                ]
            )
            manager = KernelManager(kernel_name="python3")
            manager.kernel_spec.argv[0] = sys.executable
            client = NotebookClient(
                notebook, km=manager, timeout=60, resources={"metadata": {"path": str(Path.cwd())}}
            )
            # NotebookClient.execute is synchronous; server thread remains live.
            import asyncio

            await asyncio.to_thread(client.execute)
            nbformat.write(notebook, tmp_path / "executed.ipynb")
            public = nbformat.writes(notebook)
        else:
            app = tmp_path / "native_marimo.py"
            body = "\n".join("    " + line for line in code.strip().splitlines())
            app.write_text(
                "import marimo\napp=marimo.App()\n@app.cell\nasync def _():\n"
                + body
                + '\n    ui=MarimoLauncher()\n    ui.render()\n    return\nif __name__=="__main__": app.run()\n'
            )
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "marimo",
                    "export",
                    "html",
                    str(app),
                    "-o",
                    str(tmp_path / "proof.html"),
                    "--no-include-code",
                ],
                capture_output=True,
                text=True,
                timeout=90,
                env=os.environ.copy(),
            )
            assert result.returncode == 0, result.stderr[-3000:]
            public = (tmp_path / "proof.html").read_text()
        assert proof.exists()
        assert json.loads(proof.read_text())["results_committed"]
        assert "POSTER_SESSION_KEY" not in public and "DRIVE_FIXTURE_ONLY" not in public
        await facade.close()
