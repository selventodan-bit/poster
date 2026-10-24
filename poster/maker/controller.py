"""Maker operations on an asyncio worker; no desktop framework imports."""

import asyncio

from .inspector import Inspector
from .model import GraphError
from .recorder import ActionRecorder
from .runtime import Context, GraphRunner


class MakerController:
    def __init__(self, session_factory, emit):
        self.session_factory, self.emit = session_factory, emit
        self.recorder = self.runner = self.tools = self.inspector = None

    def session(self):
        if self.recorder and self.recorder.session.page:
            return self.recorder.session
        if self.runner and self.runner.context.session.page:
            return self.runner.context.session
        return self.tools.session if self.tools else None

    async def close(self):
        if self.recorder:
            await self.recorder.close()
            self.recorder = None
        if self.runner:
            await self.runner.close()
            self.runner = None
        if self.tools:
            await self.tools.session.close()
            self.tools = None
        self.inspector = None

    async def open_browser(self, url, vault, profile):
        session = self.session()
        if not session:
            self.tools = Context(vault, headless=False)
            self.tools.session = self.session_factory(profile)
            await self.tools.page()
            session = self.tools.session
        if self.recorder and self.recorder.accepting:
            await self.recorder.navigate(url)
        else:
            from .actions import OpenURL, open_url

            context = self.runner.context if self.runner else self.tools
            if context is None:
                context = Context(vault)
                context.session = session
            await open_url(context, OpenURL(url=url))

    async def record(self, graph, vault, profile, url, after=None):
        previous = self.session()
        if after and self.recorder and self.recorder.accepting:
            raise GraphError("RUN_BUSY")
        if not previous:
            await self.close()
        self.recorder = ActionRecorder(
            session=previous or self.session_factory(profile),
            vault=vault,
            graph=graph if after else None,
            after_node=after,
            on_change=lambda data: self.emit("graph", data),
            on_warning=lambda data: self.emit("warning", data),
        )
        self.runner = self.tools = self.inspector = None
        self.recorder.graph.metadata = dict(graph.metadata)
        await self.recorder.start()
        if url:
            if previous and previous.page.url == url:
                from .navigation import sensitive_url

                self.recorder.append(
                    "Browser.OpenURL", {"url": vault.put(url) if sensitive_url(url) else url}
                )
            else:
                await self.recorder.navigate(url)
        self.emit("recording", True)

    async def stop_recording(self):
        if self.recorder:
            await self.recorder.stop(close=False)
        self.emit("recording", False)

    async def execute(self, graph, vault, profile, *, step=False, selected=None, inputs=None):
        runner = self.runner
        if runner and runner.graph.id == graph.id and runner.state == "PAUSED":
            runner.update_graph(graph)
        else:
            adopted = self.session() if selected else None
            if not adopted:
                await self.close()
            runner = GraphRunner(
                graph,
                vault=vault,
                headless=False,
                inputs=inputs,
                on_event=lambda data: self.emit("runtime", data),
            )
            runner.context.session = adopted or self.session_factory(profile)
            self.recorder = self.tools = self.inspector = None
            self.runner = runner
        if selected:
            runner.state = "PAUSED"
            await runner.execute_selected(selected)
        elif step:
            await runner.step()
        else:
            await runner.run()
        self.emit(
            "run_state", {"state": runner.state, "reason": runner.pause_reason, "current": runner.current}
        )

    async def pick(self):
        session = self.session()
        if not session:
            raise GraphError("TAB_MISSING")
        if not self.inspector or self.inspector.session is not session:
            self.inspector = Inspector(session, on_pick=lambda data: self.emit("picked", data))
        await self.inspector.enable()

    async def check(self, selector, frames):
        if not self.inspector:
            session = self.session()
            if not session:
                raise GraphError("TAB_MISSING")
            self.inspector = Inspector(session)
        self.emit("selector_result", await self.inspector.check(selector, frames))

    async def stop(self):
        if self.runner:
            await self.runner.stop()
        await self.close()
        self.emit("recording", False)
        self.emit("run_state", {"state": None, "current": None, "reason": "stopped"})

    def pause(self):
        if self.runner:
            self.runner.pause()

    def set_variable(self, name, value):
        if not self.runner:
            raise GraphError("DEBUG_STATE_INVALID")
        self.runner.set_variable(name, value)

    async def probe(self):
        """Read-only DOM geometry, including frames, only for opted-in GUI tests."""
        from .record_script import SELECTOR_FUNCTION, SELECTOR_EXPRESSION

        session = self.session()
        if not session or not session.page:
            self.emit("browser_probe", {})
            return
        page = await session.provider.active_page() if session.provider.name == "qt" else session.page
        script = """()=>{const selector=SELECTOR;return {title:document.title,viewport:{width:innerWidth,height:innerHeight},
          controls:[...document.querySelectorAll('button,a,input:not([type=hidden]),textarea,select')].map(e=>{
          const r=e.getBoundingClientRect();return {selector:selector(e),x:r.x,y:r.y,width:r.width,height:r.height};
          }).filter(r=>r.width>0&&r.height>0)}}""".replace("SELECTOR", SELECTOR_FUNCTION)
        result = await page.evaluate(script)
        for frame in page.frames:
            if frame == page.main_frame:
                continue
            try:
                element = await frame.frame_element()
                box = await element.bounding_box()
                if not box:
                    continue
                border = await element.evaluate(
                    "e=>({x:e.clientLeft,y:e.clientTop,width:e.clientWidth,height:e.clientHeight})"
                )
                clip = {
                    "x": box["x"] + border["x"],
                    "y": box["y"] + border["y"],
                    "width": border["width"],
                    "height": border["height"],
                }
                child = await frame.evaluate(script)
                path, current = [], frame
                while current.parent_frame:
                    path.insert(0, await (await current.frame_element()).evaluate(SELECTOR_EXPRESSION))
                    current = current.parent_frame
                    if current.parent_frame:
                        ancestor = await (await current.frame_element()).bounding_box()
                        if ancestor:
                            right = min(clip["x"] + clip["width"], ancestor["x"] + ancestor["width"])
                            bottom = min(clip["y"] + clip["height"], ancestor["y"] + ancestor["height"])
                            clip["x"], clip["y"] = (
                                max(clip["x"], ancestor["x"]),
                                max(clip["y"], ancestor["y"]),
                            )
                            clip["width"], clip["height"] = (
                                max(0, right - clip["x"]),
                                max(0, bottom - clip["y"]),
                            )
                for control in child["controls"]:
                    control["clip"] = clip
                    control["selector"] = " >> ".join([*path, control["selector"]])
                    control["x"] += box["x"] + border["x"]
                    control["y"] += box["y"] + border["y"]
                    result["controls"].append(control)
            except Exception:
                pass
        self.emit("browser_probe", result)


class Worker:
    def __init__(self, emit):
        import threading

        self.emit = emit
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        self.tasks = set()

    def submit(self, coroutine, *, name="operation", busy=True):
        async def work():
            task = asyncio.current_task()
            self.tasks.add(task)
            try:
                value = await coroutine
                self.emit("done", {"name": name, "value": value})
            except asyncio.CancelledError:
                self.emit("cancelled", name)
            except Exception as error:
                self.emit(
                    "error",
                    {
                        "code": error.code
                        if isinstance(error, GraphError)
                        else str(error)
                        if isinstance(error, ValueError) and len(str(error)) < 80
                        else "BROWSER_ERROR",
                        "type": type(error).__name__,
                    },
                )
            finally:
                self.tasks.discard(task)
                if busy:
                    self.emit("idle", name)

        return asyncio.run_coroutine_threadsafe(work(), self.loop)
