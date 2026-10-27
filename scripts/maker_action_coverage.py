"""Opt-in pytest evidence of original action calls made by the real GraphRunner."""

from contextvars import ContextVar
from dataclasses import replace
import json
from pathlib import Path

active = ContextVar("poster_original_runner_action", default=False)
executed = {}
expected = set()


def pytest_addoption(parser):
    parser.addoption("--maker-action-report", default=None)
    parser.addoption("--require-all-maker-actions", action="store_true")


def pytest_configure(config):
    if not config.getoption("--maker-action-report"):
        return
    from poster.maker.actions import Registry, default_registry
    from poster.maker.runtime import GraphRunner

    expected.update(a.name for a in default_registry().actions.values())
    register, execute = Registry.register, GraphRunner.execute

    def observed_register(self, action):
        original = action.execute

        async def observed(context, params):
            if not active.get():
                return await original(context, params)
            row = executed.setdefault(action.name, dict(calls=0, returned=0, errors=0))
            row["calls"] += 1
            try:
                result = await original(context, params)
                row["returned"] += 1
                return result
            except BaseException:
                row["errors"] += 1
                raise

        return register(self, replace(action, execute=observed))

    async def observed_execute(self, node, isolated=False):
        token = active.set(True)
        try:
            return await execute(self, node, isolated=isolated)
        finally:
            active.reset(token)

    Registry.register, GraphRunner.execute = observed_register, observed_execute
    config._poster_action_originals = Registry, register, GraphRunner, execute


def pytest_sessionfinish(session, exitstatus):
    path = session.config.getoption("--maker-action-report")
    if not path:
        return
    missing = sorted(name for name in expected if not executed.get(name, {}).get("returned"))
    report = dict(
        format="poster.original-actions.evidence.v1",
        registered=len(expected),
        returned=len(expected) - len(missing),
        missing=missing,
        actions={name: executed.get(name, dict(calls=0, returned=0, errors=0)) for name in sorted(expected)},
    )
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2))
    print(
        "\n::notice title=Original Maker actions::"
        + json.dumps({k: report[k] for k in ("registered", "returned", "missing")})
    )
    if session.config.getoption("--require-all-maker-actions") and (len(expected) != 78 or missing):
        session.exitstatus = 1
    Registry, register, GraphRunner, execute = session.config._poster_action_originals
    Registry.register, GraphRunner.execute = register, execute
