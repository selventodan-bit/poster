"""Publish only test names/counts from private JUnit output, never traceback values."""

import argparse
import json
from pathlib import Path
import xml.etree.ElementTree as ET


def summarize(source, target, label="Windows"):
    root = ET.parse(source).getroot()
    cases = root.findall(".//testcase")
    failures = [
        {
            "test": c.get("classname", "") + "." + c.get("name", ""),
            "kind": "error" if c.find("error") is not None else "failure",
        }
        for c in cases
        if c.find("error") is not None or c.find("failure") is not None
    ]
    report = {
        "tests": len(cases),
        "failures": failures,
        "skipped": sum(c.find("skipped") is not None for c in cases),
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, indent=2), encoding="utf-8")
    for failure in failures:
        print("::error::Windows pytest " + failure["kind"] + " " + failure["test"], flush=True)
    print("::notice title=" + label + " pytest summary::" + json.dumps(report), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("target", type=Path)
    parser.add_argument("--label", default="Windows")
    args = parser.parse_args()
    summarize(args.source, args.target, args.label)
