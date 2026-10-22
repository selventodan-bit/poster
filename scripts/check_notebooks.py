"""Optional: install .[notebook] and execute finite HTTP smoke notebooks locally."""

import os
from pathlib import Path

import nbformat
from nbclient import NotebookClient

root = Path(__file__).resolve().parents[1]
output = root / ".poster/notebooks"
output.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("IPYTHONDIR", str(root / ".poster/ipython"))
for source in sorted((root / "notebooks").glob("*.ipynb")):
    notebook = nbformat.read(source, as_version=4)
    NotebookClient(
        notebook,
        timeout=90,
        kernel_name=os.getenv("POSTER_NOTEBOOK_KERNEL", "python3"),
        resources={"metadata": {"path": str(root)}},
    ).execute()
    nbformat.write(notebook, output / source.name)
    for cell in notebook.cells:
        for item in cell.get("outputs", []):
            if item.output_type == "stream":
                print(item.text.strip())
    print(f"PASS {source.name}")
