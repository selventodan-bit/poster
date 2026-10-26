"""Build two reusable launchers pinned to reviewed code and SHA256 dependencies."""

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]


def generate(commit, archive_sha256, output):
    if not re.fullmatch("[a-f0-9]{40}", commit) or not re.fullmatch("[a-f0-9]{64}", archive_sha256):
        raise ValueError("Full source commit and archive SHA256 are required")
    bootstrap = (ROOT / "scripts/cloud/bootstrap.py").read_bytes()
    requirements = (ROOT / "requirements-cloud-runtime.txt").read_bytes()
    bootstrap_sha = hashlib.sha256(bootstrap).hexdigest()
    requirements_sha = hashlib.sha256(requirements).hexdigest()
    install = f'''# Trusted immutable source. No main/HEAD checkout and no embedded credentials.
import hashlib
import urllib.request
_BOOTSTRAP_URL = "https://raw.githubusercontent.com/selventodan-bit/poster/{commit}/scripts/cloud/bootstrap.py"
with urllib.request.urlopen(_BOOTSTRAP_URL, timeout=60) as _response:
    _bootstrap = _response.read(100_000)
assert hashlib.sha256(_bootstrap).hexdigest() == "{bootstrap_sha}", "BOOTSTRAP_INTEGRITY"
_scope = {{"__name__": "poster_cloud_bootstrap"}}
exec(compile(_bootstrap, "verified_poster_bootstrap", "exec"), _scope)
_scope["install"]("{commit}", "{archive_sha256}", "{requirements_sha}", molab=__MOLAB__)
'''

    def cell(kind, source):
        value = dict(cell_type=kind, metadata={}, source=source.splitlines(keepends=True))
        if kind == "code":
            value.update(execution_count=None, outputs=[])
        return value

    notebook = dict(
        nbformat=4,
        nbformat_minor=5,
        metadata=dict(
            kernelspec=dict(display_name="Python 3", language="python", name="python3"),
            poster=dict(
                source_commit=commit, source_sha256=archive_sha256, requirements_sha256=requirements_sha
            ),
        ),
        cells=[
            cell(
                "markdown",
                "# Poster Cloud Launcher — один notebook для всех проектов\n\nColab / обычный Jupyter. Подключите Drive, выберите проект и точную версию. Запуск начинается по кнопке.\nРежим **DETACHED**: Windows может быть выключен. Результаты шифруются для Maker и импортируются отдельно.\n\nColab: используйте интерактивный notebook, без SSH, удалённых Agents и обхода UI. Ресурсы и срок сессии не гарантированы; параллельность ограничена 1.\nЗащищённые значения добавьте через **Secrets** провайдера, а в Launcher укажите только их имена. Не вставляйте значения в ячейки.\n",
            ),
            cell("code", install.replace("__MOLAB__", "False")),
            cell(
                "code",
                'import importlib.util\nfrom IPython.display import display\nfrom poster.cloud.launcher import jupyter\n_provider = "colab" if importlib.util.find_spec("google.colab") is not None else "generic"\nposter_launcher = jupyter(_provider)\ndisplay(poster_launcher.widget)\n',
            ),
            cell(
                "markdown",
                "После завершения нажмите **Завершить сессию**. Для нового разрешённого запуска используйте **Подготовить новый запуск**.\nПри потере сессии предыдущий POST автоматически не повторяется. Проверка последствий и ручное разрешение описаны в руководстве.\nВ Dispatcher: Google Drive → Импортировать автономные результаты.\n",
            ),
        ],
    )
    for i, c in enumerate(notebook["cells"]):
        c["id"] = "poster-launcher-" + str(i)
    output.mkdir(parents=True, exist_ok=True)
    (output / "Poster_Launcher.ipynb").write_text(
        json.dumps(notebook, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )
    molab = (
        """import marimo
__generated_with = "0.25.2"
app = marimo.App(width="medium")

@app.cell
def _():
"""
        + "".join("    " + line + "\n" for line in install.replace("__MOLAB__", "True").splitlines())
        + """    return

@app.cell
def _():
    from poster.cloud.marimo_frontend import MarimoLauncher
    launcher = MarimoLauncher("molab")
    return (launcher,)

@app.cell
def _(launcher):
    launcher.render()
    return

if __name__ == "__main__":
    app.run()
"""
    )
    (output / "Poster_Molab.py").write_text(molab, encoding="utf-8")
    subprocess.run(
        [
            sys.executable,
            "-m",
            "ruff",
            "format",
            str(output / "Poster_Molab.py"),
            str(output / "Poster_Launcher.ipynb"),
        ],
        check=True,
    )
    pins = dict(
        format="poster.notebook.release.v1",
        source_commit=commit,
        source_archive_sha256=archive_sha256,
        bootstrap_sha256=bootstrap_sha,
        requirements_sha256=requirements_sha,
        gdrive_fsspec_commit="dd9bb3025b26091d4a29ac4e81cc3d88910203bd",
        files={
            p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in output.glob("Poster_*") if p.is_file()
        },
    )
    (output / "poster-launcher-pins.json").write_text(json.dumps(pins, indent=2) + "\n")
    return pins


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--commit", required=True)
    parser.add_argument("--source-sha256", required=True)
    parser.add_argument("--output", type=Path, default=ROOT / "notebooks")
    args = parser.parse_args()
    print(json.dumps(generate(args.commit, args.source_sha256, args.output)))
