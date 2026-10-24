"""Browser extraction and explicit tab management, shared by GUI and headless runs."""

import re

from pydantic import Field

from .actions import Action, Empty, OpenURL, Property, Target, open_url
from .model import DataModel, GraphError
from .navigation import sensitive_url


class Attribute(Target):
    attribute: str = Field(default="href", min_length=1, max_length=200)


class Tab(DataModel):
    index: int = Field(default=0, ge=0, le=100)
    tab_id: str = ""


class NewTab(OpenURL):
    url: str = ""
    tab_id: str = ""


class Popup(DataModel):
    tab_id: str
    after_node: str
    timeout_ms: int = Field(default=10000, ge=1, le=120000)


async def text(ctx, p):
    return "success", {"value": (await (await ctx.locator(p)).inner_text(timeout=p.timeout_ms))[:200000]}


async def attribute(ctx, p):
    target = await ctx.locator(p)
    value = await target.get_attribute(p.attribute, timeout=p.timeout_ms)
    sensitive = bool(re.search(r"pass|secret|token|cookie|authorization", p.attribute, re.I))
    sensitive |= bool(value and sensitive_url(value))
    if p.attribute == "value":
        sensitive |= await target.get_attribute("type", timeout=p.timeout_ms) in {"password", "hidden"}
    if sensitive and value is not None:
        ctx.sensitive_action = True
        value = ctx.vault.put(value)
    return "success", {"value": value}


async def new_tab(ctx, p):
    await ctx.page()
    page = await ctx.session.new_page(p.tab_id or None)
    ctx.use_page(page)
    if p.url:
        await open_url(ctx, OpenURL(url=p.url))
    return "success", {"index": ctx.session.context.pages.index(page)}


async def switch_tab(ctx, p):
    await ctx.page()
    pages = ctx.session.context.pages
    if p.tab_id:
        page = ctx.use_page(await ctx.session.select_tab(p.tab_id))
        return "success", {"tab_id": p.tab_id, "index": pages.index(page)}
    if p.index >= len(pages):
        raise GraphError("TAB_MISSING")
    page = ctx.use_page(pages[p.index])
    await ctx.session.provider.activate(page)
    return "success", {"index": p.index}


async def close_tab(ctx, p):
    page = await ctx.page()
    await ctx.session.provider.close_page(page)
    pages = ctx.session.context.pages
    ctx.use_page(pages[-1] if pages else await ctx.session.new_page())
    return "success", {"remaining": len(ctx.session.context.pages)}


async def wait_popup(ctx, p):
    checkpoint = ctx.checkpoints.get(p.after_node)
    if checkpoint is None:
        raise GraphError("WAIT_ANCHOR_MISSING")
    opener = checkpoint[1]
    previous = ctx.popup_checkpoints.get(p.after_node, set())
    import asyncio

    deadline = asyncio.get_running_loop().time() + p.timeout_ms / 1000
    while asyncio.get_running_loop().time() < deadline:
        for page in ctx.session.context.pages:
            if page in previous or page.is_closed():
                continue
            actual_opener = await page.opener()
            if actual_opener is None and ctx.session.provider.name == "qt":
                provider = ctx.session.provider
                state = await provider.request("state")
                target, parent = await provider.target(page), await provider.target(opener)
                actual_opener = (
                    opener
                    if any(t["target"] == target and t["opener"] == parent for t in state["tabs"])
                    else None
                )
            if actual_opener is opener:
                ctx.session.bind_tab(p.tab_id, page)
                ctx.use_page(page)
                await ctx.session.provider.activate(page)
                await page.wait_for_load_state("domcontentloaded", timeout=p.timeout_ms)
                return "success", {"tab_id": p.tab_id}
        await asyncio.sleep(0.05)
    raise GraphError("POPUP_TIMEOUT")


def register_browser_actions(registry):
    target = (
        Property("selector", "CSS или xpath=…"),
        Property("frame", "Iframe по одному на строку", "lines"),
        Property("timeout_ms", "Ожидание, мс", "integer"),
    )
    for action in (
        Action(
            "Browser.WaitPopup",
            "Вкладки · Ждать popup",
            Popup,
            wait_popup,
            (
                Property("tab_id", "ID новой вкладки"),
                Property("after_node", "ID вызвавшего кубика"),
                Property("timeout_ms", "Ожидание, мс", "integer"),
            ),
        ),
        Action("Browser.ReadText", "Браузер · Читать текст", Target, text, target),
        Action(
            "Browser.ReadAttribute",
            "Браузер · Читать атрибут",
            Attribute,
            attribute,
            (*target, Property("attribute", "Атрибут")),
        ),
        Action(
            "Browser.NewTab",
            "Вкладки · Открыть",
            NewTab,
            new_tab,
            (Property("url", "URL (необязательно)"), Property("tab_id", "ID новой вкладки (необязательно)")),
        ),
        Action(
            "Browser.SwitchTab",
            "Вкладки · Переключить",
            Tab,
            switch_tab,
            (
                Property("index", "Индекс вкладки (с нуля)", "integer"),
                Property("tab_id", "ID вкладки (вместо индекса)"),
            ),
        ),
        Action("Browser.CloseTab", "Вкладки · Закрыть текущую", Empty, close_tab),
    ):
        registry.register(action)
