"""Shell-open a frozen app with only Windows system commands on its PATH."""

import os
from pathlib import Path


def open_portable(executable):
    # Restrict the child only; the external GUI test harness keeps its Python/tools.
    previous = {key: os.environ.get(key) for key in ("PATH", "POSTER_PYTHON", "PYTHONHOME", "PYTHONPATH")}
    try:
        windows = Path(os.environ["SystemRoot"])
        os.environ["PATH"] = os.pathsep.join(map(str, (windows / "System32", windows)))
        for key in ("POSTER_PYTHON", "PYTHONHOME", "PYTHONPATH"):
            os.environ.pop(key, None)
        os.startfile(str(executable))  # ShellExecute, as with double-clicking Poster.exe.
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
