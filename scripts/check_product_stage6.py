"""Cross-platform selection with no shell glob assumptions or secret-bearing reports."""

import argparse
from pathlib import Path
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("suite", choices=["recovery", "actions", "gui"])
    suite = parser.parse_args().suite
    options = []
    if suite == "recovery":
        tests = [str(p) for p in sorted(Path("tests").glob("test_product_*.py"))]
        tests += ["tests/test_cloud_auth.py", "tests/test_dispatcher_host.py"]
    elif suite == "actions":
        tests = [
            "tests/test_maker.py",
            "tests/test_maker_stage2.py",
            "tests/test_maker_stage3a.py",
            "tests/test_maker_actions3.py",
            "tests/product_actions_e2e.py",
        ]
        options = [
            "-p",
            "scripts.maker_action_coverage",
            "--maker-action-report=.poster/stage6-original-actions.json",
            "--require-all-maker-actions",
        ]
    else:
        tests = ["tests/product_qt_e2e.py"]
    result = subprocess.call(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            *options,
            *tests,
            "--junitxml=.poster/stage6-" + suite + ".xml",
        ]
    )
    if result == 0 and suite == "recovery":
        from product_recovery_receipt import receipt

        return receipt(".poster/stage6-recovery.xml", ".poster/stage6-recovery-scenarios.json")
    return result


if __name__ == "__main__":
    raise SystemExit(main())
