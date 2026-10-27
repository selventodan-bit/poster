"""Only explicit synthetic test evidence; no graph/credential/log serialization."""

import json
import os
from pathlib import Path
import platform
import subprocess


def save(name, value):
    directory = Path(__file__).resolve().parents[1] / ".poster/product-public"
    directory.mkdir(parents=True, exist_ok=True)
    report = dict(
        format="poster.product.evidence.v1",
        os=platform.system(),
        kernel=platform.release(),
        commit=subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=Path(__file__).resolve().parents[1], text=True
        ).strip(),
        source_dirty=bool(
            subprocess.check_output(
                ["git", "status", "--porcelain", "--untracked-files=no"],
                cwd=Path(__file__).resolve().parents[1],
            )
        ),
        status="PASSED",
        **value,
    )
    path = directory / (name + ".json")
    temporary = path.with_name("." + path.name + str(os.getpid()))
    temporary.write_text(json.dumps(report, indent=2))
    temporary.replace(path)
