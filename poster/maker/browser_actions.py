"""Browser extraction and explicit tab management, shared by GUI and headless runs."""

import re

from pydantic import Field

from .actions import Action, Empty, OpenURL, Property, Target, open_url
from .model import DataModel, GraphError


class Attribute(Target):
    attribute: str = Field(default="href", min_length=1, max_length=200)


class Tab(DataModel):
    index: int = Field(default=0, ge=0, le=100)


class NewTab(OpenURL):
    url: str = ""


async def text(ctx, p):
    return "success", {"value": (await (await ctx.locator(p)).inner_text(timeout=p.timeout_ms))[:200000]}


async def attribute(ctx, p):
    target = await ctx.locator(p)
    value = await target.get_attribute(p.attribute, timeout=p.timeout_ms)
    sensitive = bool(re.search(r"pass|secret|token|cookie|authorization", p.attribute, re.I))
    if p.attribute == "value":
        sensitive |= await target.get_attribute("type", timeout=p.timeout_ms) in {"password", "hidden"}
    if sensitive and value is not None:
        ctx.sensitive_action = True
        value = ctx.vault.put(value)
    return "success", {"value": value}


async def new_tab(ctx, p):
    await ctx.page()
    page = await ctx.session.context.new_page()
    ctx.use_page(page)
    if p.url:
        await open_url(ctx, OpenURL(url=p.url))
    return "success", {"index": ctx.session.context.pages.index(page)}


async def switch_tab(ctx, p):
    await ctx.page()
    pages = ctx.session.context.pages
    if p.index >= len(pages):
        raise GraphError("TAB_MISSING")
    page = ctx.use_page(pages[p.index])
    await page.bring_to_front()
    return "success", {"index": p.index}


async def close_tab(ctx, p):
    page = await ctx.page()
    await page.close()
    pages = ctx.session.context.pages
    ctx.use_page(pages[-1] if pages else await ctx.session.context.new_page())
    return "success", {"remaining": len(ctx.session.context.pages)}


def register_browser_actions(registry):
    target = (
        Property("selector", "CSS или xpath=…"),
        Property("frame", "Iframe по одному на строку", "lines"),
        Property("timeout_ms", "Ожидание, мс", "integer"),
    )
    for action in (
        Action("Browser.ReadText", "Браузер · Читать текст", Target, text, target),
        Action(
            "Browser.ReadAttribute",
            "Браузер · Читать атрибут",
            Attribute,
            attribute,
            (*target, Property("attribute", "Атрибут")),
        ),
        Action(
            "Browser.NewTab", "Вкладки · Открыть", NewTab, new_tab, (Property("url", "URL (необязательно)"),)
        ),
        Action(
            "Browser.SwitchTab",
            "Вкладки · Переключить",
            Tab,
            switch_tab,
            (Property("index", "Индекс вкладки (с нуля)", "integer"),),
        ),
        Action("Browser.CloseTab", "Вкладки · Закрыть текущую", Empty, close_tab),
    ):
        registry.register(action)
