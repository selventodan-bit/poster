"""Console-subsystem internal launchers, hidden by the owning service.

Separate PyInstaller analysis excludes all Qt/Tk runtime hooks. An HTTP-only
worker must not import GUI bindings even before its Python entrypoint runs.
"""

from pathlib import Path
import sys


def main():
    if Path(sys.executable).stem.casefold() == "posterservice":
        from poster.dispatcher.host import main as entry
    elif Path(sys.executable).stem.casefold() == "posterworker":
        from poster.dispatcher.worker import main as entry
    else:
        return 2
    return entry()


if __name__ == "__main__":
    raise SystemExit(main())
