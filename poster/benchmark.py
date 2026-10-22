"""Repeatable controlled benchmark. Scores do not estimate success on arbitrary public forums."""

import hashlib
import json
from pathlib import Path

from .auth import ensure_local_key
from .fixtures_benchmark import serve_benchmark
from .recognition.service import RecognitionSession


def corpus():
    path = Path(__file__).parent / "recognition/data/benchmark.json"
    return json.loads(path.read_text(encoding="utf-8"))["cases"]


async def run_case(case, base, output):
    session = RecognitionSession(
        base + "/bench/" + case["id"], output / case["id"], headless=True, confirmation_timeout=1
    )
    record = {
        "id": case["id"],
        "stratum": case["stratum"],
        "form": False,
        "fields": False,
        "browser": False,
        "http": False,
        "manual": False,
        "false_confirmation": False,
    }
    try:
        await session.start()
        result = await session.recognize()
        record["form"] = bool(result.forms)
        record["manual"] = result.state != "DETECTED"
        if result.state == "DETECTED":
            await session.fill("Benchmark title " + case["id"], "Контрольный текст & Unicode " + case["id"])
            # Independent ground truth: the renderer's real submission control, not detector labels.
            actual = await session.page.evaluate("""() => {
             const root=document.querySelector('#island')?.shadowRoot||document;
             const f=root.querySelector('#composer');return f?.querySelector('#heading')?.value;
            }""")
            record["fields"] = actual == "Benchmark title " + case["id"]
            await session.test_publish(
                "Benchmark title " + case["id"],
                "Контрольный текст & Unicode " + case["id"],
                case["id"],
                confirmed=True,
            )
            record["browser"] = session.result.confidence.result == 1
            record["state"] = str(session.result.state)
            await session.close()
            if session.result.state == "HTTP_CANDIDATE":
                trial = await session.verify_http(
                    "Independent HTTP " + case["id"], "Новый текст HTTP & " + case["id"], confirmed=True
                )
                record["http"] = trial.status == "SUCCEEDED"
                record["state"] = str(session.result.state)
                record["http_reason"] = trial.reason
        else:
            record["state"] = str(result.state)
        negative = case.get("negative")
        record["false_confirmation"] = record["browser"] and negative in {
            "rejected",
            "silent",
            "hidden",
            "login",
        }
        record["diagnostics"] = session.result.diagnostics
        return record
    finally:
        await session.close()


def ratio(rows, key):
    return {
        "passed": sum(bool(r[key]) for r in rows),
        "total": len(rows),
        "rate": round(sum(bool(r[key]) for r in rows) / len(rows), 4) if rows else None,
    }


async def transfer_benchmark(output):
    from .recognition.knowledge import KnowledgeBase

    training = dict(
        id="training",
        locale="en",
        editor="textarea",
        nav="menu",
        protocol="draft",
        layout="classic",
        reorder=True,
        stratum="training",
    )
    kb = KnowledgeBase(output / "knowledge.sqlite")
    try:
        with serve_benchmark([training]) as (_, base):
            a = RecognitionSession(base + "/bench/training", output / "train", knowledge=kb, headless=True)
            try:
                await a.start()
                await a.recognize()
                await a.test_publish("Training subject", "Training body", "training", confirmed=True)
                await a.verify_http("Training HTTP subject", "Training HTTP body", confirmed=True)
            finally:
                await a.close()
        kb.close()
        kb = KnowledgeBase(output / "knowledge.sqlite")
        results = []
        for locale in ("ar", "uk", "pl"):
            case = dict(
                id="transfer-" + locale,
                locale=locale,
                editor="textarea",
                nav="icon",
                protocol="header",
                layout="classic",
                unknown=True,
                aux_first=True,
                stratum="trained",
            )
            with serve_benchmark([case]) as (_, base):
                before = RecognitionSession(
                    base + "/bench/" + case["id"], output / ("before-" + locale), headless=True
                )
                try:
                    await before.start()
                    untrained = (await before.recognize()).state == "DETECTED"
                finally:
                    await before.close()
                after = RecognitionSession(
                    base + "/bench/" + case["id"], output / ("after-" + locale), headless=True, knowledge=kb
                )
                try:
                    await after.start()
                    found = (await after.recognize()).state == "DETECTED"
                    if found:
                        await after.test_publish(
                            "New site " + locale, "New site body " + locale, case["id"], confirmed=True
                        )
                        await after.close()
                        trial = await after.verify_http(
                            "HTTP new site " + locale, "HTTP new site body " + locale, confirmed=True
                        )
                        success = trial.status == "SUCCEEDED"
                    else:
                        success = False
                    results.append(
                        {
                            "locale": locale,
                            "untrained_detected": untrained,
                            "trained_detected": found,
                            "http": success,
                        }
                    )
                finally:
                    await after.close()
        # Re-run the source variant after all adaptations; there must be no regression.
        with serve_benchmark([training]) as (_, base):
            reg = RecognitionSession(
                base + "/bench/training", output / "regression", knowledge=kb, headless=True
            )
            try:
                await reg.start()
                detected = (await reg.recognize()).state == "DETECTED"
                await reg.test_publish("Regression subject", "Regression body", "regression", confirmed=True)
                await reg.close()
                passed = (
                    detected
                    and (
                        await reg.verify_http(
                            "Regression HTTP subject", "Regression HTTP body", confirmed=True
                        )
                    ).status
                    == "SUCCEEDED"
                )
            finally:
                await reg.close()
        return {
            "rows": results,
            "successes": sum(r["http"] for r in results),
            "total": len(results),
            "regressions": int(not passed),
            "regression_trials": 1,
        }
    finally:
        kb.close()


async def control_benchmark(output):
    cases = [
        dict(id="decoy-" + d, locale="en", editor="textarea", protocol="json", decoy=d)
        for d in ("login", "search", "profile", "contact", "reply")
    ]
    false_positives = 0
    with serve_benchmark(cases) as (server, base):
        for case in cases:
            session = RecognitionSession(base + "/bench/" + case["id"], output / case["id"], headless=True)
            try:
                await session.start()
                false_positives += (await session.recognize()).state == "DETECTED"
            finally:
                await session.close()
        return {
            "false_positives": false_positives,
            "total": len(cases),
            "rate": false_positives / len(cases),
            "writes": len(server.writes),
        }


async def known_benchmark(output):
    from .fixtures_recognition import serve_recognition_fixture

    records = []
    with serve_recognition_fixture() as (_, base):
        for kind in ("classic", "ajax", "json", "multipart", "contenteditable", "iframe", "csrf"):
            session = RecognitionSession(base + "/recognize/" + kind, output / kind, headless=True)
            try:
                await session.start()
                found = (await session.recognize()).state == "DETECTED"
                await session.test_publish("Known title " + kind, "Known body " + kind, kind, confirmed=True)
                browser = session.result.confidence.result == 1
                await session.close()
                trial = await session.verify_http(
                    "Known HTTP title " + kind, "Known HTTP body " + kind, confirmed=True
                )
                records.append(
                    {"id": kind, "form": found, "browser": browser, "http": trial.status == "SUCCEEDED"}
                )
            finally:
                await session.close()
    return {"rows": records, **{k: ratio(records, k) for k in ("form", "browser", "http")}}


async def benchmark(output: Path):
    output.mkdir(parents=True, exist_ok=True)
    ensure_local_key(output / "private")
    cases = corpus()
    records = []
    with serve_benchmark(cases) as (server, base):
        for case in cases:
            records.append(await run_case(case, base, output / "profiles"))
        report = {
            "corpus_sha256": hashlib.sha256(json.dumps(cases, sort_keys=True).encode()).hexdigest(),
            "scope": "controlled synthetic held-out variants; no public CMS support claim",
            "rows": records,
            "strata": {},
        }
        for stratum in sorted({r["stratum"] for r in records}):
            rows = [r for r in records if r["stratum"] == stratum]
            report["strata"][stratum] = {
                k: ratio(rows, k)
                for k in ("form", "fields", "browser", "http", "manual", "false_confirmation")
            }
        report["unexpected_mutations"] = sum(p.endswith("/side-effect") for p in server.writes)
        report["publications"] = len(server.posts)
    report["known"] = await known_benchmark(output / "known")
    report["transfer"] = await transfer_benchmark(output / "transfer")
    report["false_positive_rate"] = await control_benchmark(output / "controls")
    from .recognition.detector import EngineDetector

    engines = json.loads((Path(__file__).parent / "recognition/data/engines.json").read_text())
    report["engine_signature_corpus"] = {
        "scope": "curated synthetic markup; identification only",
        "passed": sum(
            {t.family for t in EngineDetector().inspect(c["html"])} == set(c["expected"]) for c in engines
        ),
        "total": len(engines),
    }
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report
