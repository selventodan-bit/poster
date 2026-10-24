"""Extensible action registry. Graph data never imports or evaluates Python/JS."""

from dataclasses import dataclass
import json
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BeforeValidator, Field, model_validator

from .model import DataModel, GraphError


class SecretRef(DataModel):
    secret_ref: str = Field(min_length=1, max_length=200)


def text_value(value):
    if value is None:
        return ""
    return json.dumps(value) if isinstance(value, (int, float, bool)) else value


Value = Annotated[str, BeforeValidator(text_value)] | SecretRef


class Empty(DataModel):
    pass


class OpenURL(DataModel):
    url: Value = "https://example.org"


class Target(DataModel):
    selector: str = Field(default="body", min_length=1, max_length=4000)
    frame: list[str] = Field(default_factory=list, max_length=8)
    timeout_ms: int = Field(default=10000, ge=50, le=120000)


class Click(Target):
    button: Literal["left", "right", "middle"] = "left"


class Input(Target):
    value: Value = ""
    sensitive: bool = False

    @model_validator(mode="after")
    def protected(self):
        if self.sensitive and not isinstance(self.value, SecretRef):
            raise ValueError("PASSWORD_REF_REQUIRED")
        return self


class Select(Target):
    values: list[str] = Field(default_factory=lambda: ["one"], max_length=100)


class WaitElement(Target):
    state: Literal["visible", "attached", "hidden", "detached"] = "visible"


class WaitURL(DataModel):
    url: Value = "https://example.org/**"
    timeout_ms: int = Field(default=10000, ge=50, le=120000)
    after_action: bool = False
    after_node: str | None = None


class If(Target):
    condition: Literal["exists", "not_exists", "text_contains", "url_contains", "equals"] = "exists"
    expected: Value = ""
    left: Value = ""


@dataclass(frozen=True)
class Property:
    name: str
    label: str
    kind: str = "text"
    choices: tuple = ()
    secret: bool = False
    help: str = ""


@dataclass(frozen=True)
class Action:
    name: str
    label: str
    params: type
    execute: object
    properties: tuple = ()
    version: int = 1
    reveal_parameters: bool = False


class Registry:
    def __init__(self):
        self.actions = {}

    def register(self, action):
        key = (action.name, action.version)
        if key in self.actions:
            raise GraphError("ACTION_ALREADY_REGISTERED")
        self.actions[key] = action

    def get(self, name, version=1):
        try:
            return self.actions[name, version]
        except KeyError as exc:
            raise GraphError("ACTION_UNSUPPORTED") from exc

    def parameters(self, node, values=None):
        try:
            spec = self.get(node.type, node.action_version)
            return spec, spec.params.model_validate(node.params if values is None else values)
        except GraphError:
            raise
        except ValueError as exc:
            raise GraphError("PARAMETERS_INVALID", node.id) from exc


def resolve(ctx, value):
    return ctx.vault.get(value.secret_ref) if isinstance(value, SecretRef) else value


def resolve_text(ctx, value):
    result = text_value(resolve(ctx, value))
    if not isinstance(result, str):
        raise GraphError("VARIABLE_TYPE_INVALID")
    return result


async def no_op(ctx, params):
    return "success", {}


async def open_url(ctx, params):
    url = resolve_text(ctx, params.url)
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise GraphError("URL_INVALID")
    page = await ctx.before_action()
    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    return "success", {"opened": True}


async def click(ctx, params):
    locator = await ctx.locator(params)
    await ctx.before_action()
    await ctx.session.provider.prepare_pointer(locator, params.timeout_ms)
    await locator.click(button=params.button, timeout=params.timeout_ms)
    return "success", {"clicked": True}


async def fill(ctx, params):
    locator = await ctx.locator(params)
    if await locator.get_attribute("type", timeout=params.timeout_ms) == "password" and not isinstance(
        params.value, SecretRef
    ):
        raise GraphError("PASSWORD_REF_REQUIRED")
    await ctx.before_action()
    value = resolve_text(ctx, params.value)
    await locator.fill(value, timeout=params.timeout_ms)
    return "success", {"filled": True, "protected": isinstance(params.value, SecretRef)}


async def select(ctx, params):
    await ctx.before_action()
    await (await ctx.locator(params)).select_option(value=params.values, timeout=params.timeout_ms)
    return "success", {"selected": True}


async def wait_element(ctx, params):
    await (await ctx.locator(params)).wait_for(state=params.state, timeout=params.timeout_ms)
    return "success", {"state": params.state}


async def wait_url(ctx, params):
    return "success", await ctx.wait_url(
        resolve_text(ctx, params.url), params.timeout_ms, params.after_action, params.after_node
    )


async def condition(ctx, params):
    expected = resolve_text(ctx, params.expected)
    if params.condition == "equals":
        value = resolve_text(ctx, params.left) == expected
    elif params.condition == "url_contains":
        value = expected in (await ctx.page()).url
    else:
        locator = await ctx.locator(params)
        if params.condition in {"exists", "not_exists"}:
            value = bool(await locator.count())
            if params.condition == "not_exists":
                value = not value
        else:
            value = expected in await locator.inner_text(timeout=params.timeout_ms)
    return "success" if value else "error", {"condition": value}


def default_registry():
    registry = Registry()
    selector = Property("selector", "Селектор элемента")
    frame = Property("frame", "Iframe: селекторы по одному на строку", "lines")
    timeout = Property("timeout_ms", "Ожидание, мс", "integer")
    target = (selector, frame, timeout)
    for spec in (
        Action("Start", "Start — начало", Empty, no_op),
        Action("End", "End — завершение", Empty, no_op),
        Action("Browser.OpenURL", "Открыть URL", OpenURL, open_url, (Property("url", "URL", secret=True),)),
        Action(
            "Browser.Click",
            "Клик",
            Click,
            click,
            (*target, Property("button", "Кнопка мыши", "choice", ("left", "right", "middle"))),
        ),
        Action(
            "Browser.Input",
            "Ввод текста",
            Input,
            fill,
            (*target, Property("value", "Текст / ссылка на секрет", "multiline", secret=True)),
        ),
        Action(
            "Browser.Select",
            "Выбор из списка",
            Select,
            select,
            (*target, Property("values", "Значения: по одному на строку", "lines")),
        ),
        Action(
            "Browser.WaitElement",
            "Ждать элемент",
            WaitElement,
            wait_element,
            (
                *target,
                Property("state", "Состояние", "choice", ("visible", "attached", "hidden", "detached")),
            ),
        ),
        Action(
            "Browser.WaitURL",
            "Ждать переход",
            WaitURL,
            wait_url,
            (
                Property("url", "URL / маска", secret=True),
                timeout,
                Property("after_action", "Требовать переход после действия", "bool"),
                Property("after_node", "Переход после кубика (необязательно)", "node"),
            ),
        ),
        Action(
            "Logic.If",
            "Условие If",
            If,
            condition,
            (
                *target,
                Property(
                    "condition",
                    "Условие",
                    "choice",
                    ("exists", "not_exists", "text_contains", "url_contains", "equals"),
                ),
                Property("expected", "Ожидаемое значение", secret=True),
                Property("left", "Левое значение для equals", secret=True),
            ),
        ),
    ):
        registry.register(spec)
    from .control_actions import register_control_actions
    from .browser_actions import register_browser_actions

    register_control_actions(registry)
    register_browser_actions(registry)
    from .http_actions import register_http_actions

    register_http_actions(registry)
    from .data_actions import register_data_actions

    register_data_actions(registry)
    from .file_actions import register_file_actions

    register_file_actions(registry)
    return registry
