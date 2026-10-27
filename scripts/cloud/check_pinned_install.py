"""Fresh-venv execution of the shipped, immutable notebook installer (no accounts)."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import venv

ROOT = Path(__file__).resolve().parents[2]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    pins = json.loads((ROOT / "notebooks/poster-launcher-pins.json").read_text())
    notebook = json.loads((ROOT / "notebooks/Poster_Launcher.ipynb").read_text())
    # Every installed Python module must match this delivery checkout.
    runtime_hashes = {
        p.relative_to(ROOT / "poster").as_posix(): hashlib.sha256(
            p.read_bytes().replace(b"\r\n", b"\n")
        ).hexdigest()
        for p in (ROOT / "poster").rglob("*.py")
    }
    install = "".join(notebook["cells"][1]["source"])
    with tempfile.TemporaryDirectory(prefix="poster-pinned-install-") as temp:
        root = Path(temp)
        envdir = root / "env"
        venv.EnvBuilder(with_pip=True).create(envdir)
        python = envdir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        proof = args.output.resolve() / "pinned-install.json"
        program = (
            install
            + f"""
_scope["install"]({pins["source_commit"]!r},{pins["source_archive_sha256"]!r},{pins["requirements_sha256"]!r},molab=True)
import importlib.metadata as metadata
import json
from pathlib import Path
import poster
from poster.maker.actions import default_registry
from poster.maker.example_projects import chain
from poster.maker.storage import save_graph,Vault
from poster.dispatcher.security import key_setup
from poster.dispatcher.store import Store
from poster.dispatcher.service import Dispatcher,enqueue
from gdrive_fsspec import GoogleDriveFileSystem
assert Path(poster.__file__).is_relative_to(Path({str(envdir)!r}))
assert len(default_registry().actions)==78
import hashlib
_expected_runtime={runtime_hashes!r}
for _name,_sha in _expected_runtime.items():
 assert hashlib.sha256((Path(poster.__file__).parent/_name).read_bytes().replace(b"\\r\\n",b"\\n")).hexdigest()==_sha, "RUNTIME_SOURCE_MISMATCH: "+_name
root=Path({str(root / "smoke")!r});root.mkdir()
source=root/'project.postergraph.json'
save_graph(source,chain('Installed immutable code',[('Files.WriteText',{{'path':'out.txt','text':'pinned'}})]),Vault())
key_setup(root/'state');store=Store(root/'dispatcher.sqlite')
job=enqueue(store,source);Dispatcher(store,max_workers=1).run(once=True)
assert store.show(job)['status']=='SUCCEEDED'
store.close()
Path({str(proof)!r}).write_text(json.dumps(dict(status='PASSED',source_commit={pins["source_commit"]!r},native_worker='SUCCEEDED',matching_runtime_modules=len(_expected_runtime),packages={{n:metadata.version(n) for n in ['poster-http','marimo','ipywidgets','nbclient','gdrive_fsspec']}}),indent=2))
"""
        )
        script = root / "check.py"
        script.write_text(program)
        env = {
            k: v
            for k, v in os.environ.items()
            if k
            not in {
                "POSTER_SESSION_KEY",
                "PYTHONPATH",
                "PYTHONHOME",
                "GH_TOKEN",
                "GITHUB_TOKEN",
                "POSTER_CLOUD_TEST_CONFIG",
            }
        }
        env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
        env["PIP_CACHE_DIR"] = str(Path(tempfile.gettempdir()) / "poster-pinned-pip-cache")
        env["POSTER_PRODUCT_LOCK_HOME"] = str(root / "product-locks")
        with (args.output / "install-private.log").open("wb") as log:
            result = subprocess.run(
                [str(python), str(script)], cwd=root, env=env, stdout=log, stderr=log, timeout=600
            )
        if result.returncode:
            raise RuntimeError("PINNED_NOTEBOOK_INSTALL_FAILED")
        report = json.loads(proof.read_text())
        assert report["status"] == "PASSED"
        print("::notice title=Immutable notebook installation::" + json.dumps(report))


if __name__ == "__main__":
    main()
