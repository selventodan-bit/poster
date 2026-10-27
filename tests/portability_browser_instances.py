"""Five real owned Chromium processes: profiles, addressable stop, timeout and damage."""

import asyncio
import json
import os
from pathlib import Path
import platform

import psutil
import pytest

from poster.maker.example_projects import chain
from poster.maker.fixtures import serve_maker
from poster.maker.model import GraphError
from poster.maker.profiles import Profiles
from poster.maker.runtime import GraphRunner, BrowserSession


async def test_five_real_browsers_profile_isolation_cancel_timeout_and_corruption(tmp_path):
    profiles = Profiles(tmp_path / "profiles")
    sessions, runners, tasks = [], [], []
    existing = {p.pid for p in psutil.Process().children(recursive=True)}
    with serve_maker() as (_, base):
        try:
            for index in range(5):
                key = profiles.create("Independent " + str(index))
                profiles.open(key).commit(
                    dict(
                        cookies=[
                            dict(
                                name="independent",
                                value="PRIVATE_5D_COOKIE_" + str(index),
                                domain="127.0.0.1",
                                path="/",
                                httpOnly=True,
                                sameSite="Lax",
                            )
                        ],
                        origins=[
                            dict(
                                origin=base,
                                localStorage=[
                                    dict(name="independent", value="PRIVATE_5D_STORAGE_" + str(index))
                                ],
                            )
                        ],
                    )
                )
                sessions.append(BrowserSession(True, profile=profiles.open(key)))
            await asyncio.gather(*(s.start() for s in sessions))
            await asyncio.gather(*(s.page.goto(base + "/login") for s in sessions))
            assert len({id(s.context) for s in sessions}) == 5
            for index, session in enumerate(sessions):
                assert next(
                    c["value"] for c in await session.context.cookies() if c["name"] == "independent"
                ) == "PRIVATE_5D_COOKIE_" + str(index)
                assert await session.page.evaluate(
                    "localStorage.getItem('independent')"
                ) == "PRIVATE_5D_STORAGE_" + str(index)
            browsers = []
            for child in psutil.Process().children(recursive=True):
                try:
                    args = child.cmdline()
                    if (
                        child.pid not in existing
                        and "--remote-debugging-pipe" in args
                        and not any(arg.startswith("--type=") for arg in args)
                    ):
                        browsers.append(dict(pid=child.pid, birth=child.create_time()))
                    assert not any("PRIVATE_5D_" in arg for arg in args)
                except psutil.NoSuchProcess:
                    pass
            assert len(browsers) == 5, browsers
            for index in range(5):
                runner = GraphRunner(
                    chain(
                        "Instance " + str(index),
                        [
                            ("Browser.ReadText", dict(selector="body")),
                            ("Logic.Delay", dict(milliseconds=5000 if index in {0, 1} else 1500)),
                            ("Files.WriteText", dict(path="out.txt", text="public instance " + str(index))),
                        ],
                    ),
                    workdir=tmp_path / ("run-" + str(index)),
                    max_seconds=1 if index == 1 else 60,
                )
                runner.context.session = sessions[index]
                runners.append(runner)
                tasks.append(asyncio.create_task(runner.run(breakpoints=False)))
            await asyncio.sleep(0.2)
            await runners[0].close()
            damaged = sessions[4].profile
            profiles.path(damaged.key).write_bytes(b"corrupted encrypted profile")
            with pytest.raises(GraphError, match="PROFILE_INVALID"):
                await damaged.save(sessions[4])
            states = await asyncio.gather(*tasks)
            assert states[0] == "STOPPED" and states[1] == "FAILED" and states[2:] == ["SUCCEEDED"] * 3, (
                states
            )
            assert any(r.error_code == "RUN_TIMEOUT" for r in runners[1].results)
            assert not (tmp_path / "run-0/out.txt").exists() and not (tmp_path / "run-1/out.txt").exists()
            assert {(tmp_path / ("run-" + str(i)) / "out.txt").read_text() for i in range(2, 5)} == {
                "public instance " + str(i) for i in range(2, 5)
            }
            assert profiles.open(sessions[2].profile.key).data["state"]["cookies"]
            proof = dict(
                result="PASSED",
                os=platform.system(),
                commit=os.environ.get("GITHUB_SHA", "LOCAL"),
                browsers=browsers,
                actual_browser_count=5,
                independent_contexts=5,
                states=states,
                cookies_isolated=True,
                storage_isolated=True,
                outputs_isolated=True,
                cancel_addressed=True,
                timeout_addressed=True,
                corrupt_profile_isolated=True,
                argv_secret_scan="PASSED",
            )
            output = Path(".poster/portability-browser-public")
            output.mkdir(parents=True, exist_ok=True)
            (output / "five-browsers.json").write_text(json.dumps(proof, indent=2))
        finally:
            for runner in runners:
                await runner.close()
            await asyncio.gather(*(s.close() for s in sessions))
