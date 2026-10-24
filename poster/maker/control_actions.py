"""Variables and structured control flow implemented by the Python runtime."""

import asyncio
import operator
from typing import Any, Literal

from pydantic import Field

from .actions import Action, Empty, Property, no_op
from .model import DataModel, GraphError, secret_refs


class Named(DataModel):
    name: str = Field(default="value", pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,79}$")


class Set(Named):
    value: Any = ""


class Increment(Named):
    amount: float = 1.0


class Compare(DataModel):
    left: Any = 0
    right: Any = 0
    operator: Literal[
        "equals", "not_equals", "lt", "le", "gt", "ge", "contains", "starts_with", "ends_with"
    ] = "equals"


class String(DataModel):
    operation: Literal[
        "concat", "replace", "lower", "upper", "strip", "split", "join", "length", "substring"
    ] = "concat"
    value: Any = ""
    other: Any = ""
    replacement: str = ""
    separator: str = ","
    start: int = Field(default=0, ge=0, le=1_000_000)
    end: int | None = Field(default=None, ge=0, le=1_000_000)


class Switch(DataModel):
    value: Any = ""
    cases: list[Any] = Field(default_factory=lambda: ["one", "two"], max_length=20)


class Loop(DataModel):
    count: int = Field(default=3, ge=0, le=10000)
    index: str = Field(default="i", pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,79}$")
    max_iterations: int = Field(default=1000, ge=1, le=10000)


class ForEach(DataModel):
    items: list[Any] = Field(default_factory=lambda: ["one", "two"], max_length=10000)
    item: str = Field(default="item", pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,79}$")
    index: str = Field(default="i", pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,79}$")
    max_iterations: int = Field(default=1000, ge=1, le=10000)


class Delay(DataModel):
    milliseconds: int = Field(default=500, ge=0, le=120000)


async def set_value(ctx, p):
    ctx.variables.set(p.name, p.value)
    return "success", {"value": p.value}


async def get_value(ctx, p):
    return "success", {"value": ctx.variables.get(p.name)}


async def delete_value(ctx, p):
    ctx.variables.get(p.name)
    del ctx.variables.data[p.name]
    return "success", {"deleted": True}


async def increment(ctx, p):
    value = ctx.variables.get(p.name)
    protected = bool(secret_refs(value))
    value = ctx.variables.reveal(value)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise GraphError("VARIABLE_TYPE_INVALID")
    result = value + p.amount
    if isinstance(value, int) and float(p.amount).is_integer():
        result = int(result)
    ctx.sensitive_action |= protected
    ctx.variables.set(p.name, ctx.vault.put_value(result) if protected else result)
    return "success", {"value": result}


async def compare(ctx, p):
    left, right = ctx.variables.reveal(p.left), ctx.variables.reveal(p.right)
    operators = {
        "equals": operator.eq,
        "not_equals": operator.ne,
        "lt": operator.lt,
        "le": operator.le,
        "gt": operator.gt,
        "ge": operator.ge,
        "contains": lambda a, b: b in a,
        "starts_with": lambda a, b: a.startswith(b),
        "ends_with": lambda a, b: a.endswith(b),
    }
    try:
        result = bool(operators[p.operator](left, right))
    except (TypeError, AttributeError) as exc:
        raise GraphError("VARIABLE_TYPE_INVALID") from exc
    return "success" if result else "error", {"value": result}


async def string(ctx, p):
    value, other = ctx.variables.reveal(p.value), ctx.variables.reveal(p.other)
    try:
        if p.operation == "concat":
            result = str(value) + str(other)
        elif p.operation == "replace":
            result = str(value).replace(str(other), p.replacement)
        elif p.operation == "lower":
            result = str(value).lower()
        elif p.operation == "upper":
            result = str(value).upper()
        elif p.operation == "strip":
            result = str(value).strip()
        elif p.operation == "split":
            result = str(value).split(p.separator)
        elif p.operation == "join":
            if not isinstance(value, list):
                raise GraphError("VARIABLE_TYPE_INVALID")
            result = p.separator.join(str(item) for item in value)
        elif p.operation == "length":
            result = len(value)
        else:
            result = str(value)[p.start : p.end]
    except (TypeError, ValueError) as exc:
        raise GraphError("VARIABLE_TYPE_INVALID") from exc
    return "success", {"value": result}


async def switch(ctx, p):
    value = ctx.variables.reveal(p.value)
    for index, case in enumerate(p.cases):
        candidate = ctx.variables.reveal(case)
        if isinstance(value, bool) == isinstance(candidate, bool) and value == candidate:
            return "case:" + str(index), {"case": index}
    return "success", {"default": True}


async def loop(ctx, p):
    existing = next((f for f in ctx.loops if f["node"] == ctx.current_node), None)
    if existing:
        if ctx.loops[-1] is not existing:
            raise GraphError("LOOP_MISSING")
        existing["index"] += 1
        frame = existing
    else:
        count = len(p.items) if isinstance(p, ForEach) else p.count
        if count > p.max_iterations:
            raise GraphError("LOOP_LIMIT")
        frame = {
            "node": ctx.current_node,
            "index": 0,
            "count": count,
            "items": p.items if isinstance(p, ForEach) else None,
        }
        ctx.loops.append(frame)
    if frame["index"] >= frame["count"]:
        ctx.loops.pop()
        return "error", {"completed": True, "iterations": frame["count"]}
    ctx.variables.set(p.index, frame["index"])
    if isinstance(p, ForEach):
        ctx.variables.set(p.item, frame["items"][frame["index"]])
    return "success", {"iteration": frame["index"]}


async def break_loop(ctx, p):
    if not ctx.loops:
        raise GraphError("LOOP_MISSING")
    frame = ctx.loops.pop()
    ctx.control = {"target": ctx.graph.successor(frame["node"], "error")}
    return "success", {"control": "break", "loop": frame["node"]}


async def continue_loop(ctx, p):
    if not ctx.loops:
        raise GraphError("LOOP_MISSING")
    frame = ctx.loops[-1]
    ctx.control = {"target": frame["node"]}
    return "success", {"control": "continue", "loop": frame["node"]}


async def delay(ctx, p):
    await asyncio.sleep(p.milliseconds / 1000)
    return "success", {"waited_ms": p.milliseconds}


def register_control_actions(registry):
    name = Property("name", "Имя переменной")
    value = Property("value", "Значение: текст, JSON или {{имя}}", "value", secret=True)
    limit = Property("max_iterations", "Лимит итераций", "integer")
    index = Property("index", "Переменная индекса (с нуля)")
    for action in (
        Action("Variables.Set", "Переменные · Set", Set, set_value, (name, value)),
        Action("Variables.Get", "Переменные · Get", Named, get_value, (name,)),
        Action("Variables.Delete", "Переменные · Delete", Named, delete_value, (name,)),
        Action(
            "Variables.Increment",
            "Переменные · Increment",
            Increment,
            increment,
            (name, Property("amount", "Прибавить", "value")),
        ),
        Action(
            "Variables.Compare",
            "Переменные · Compare / If",
            Compare,
            compare,
            (
                Property("left", "Левое значение / {{имя}}", "value", secret=True),
                Property(
                    "operator",
                    "Сравнение",
                    "choice",
                    tuple(Compare.model_fields["operator"].annotation.__args__),
                ),
                Property("right", "Правое значение / {{имя}}", "value", secret=True),
            ),
        ),
        Action(
            "Strings.Transform",
            "Строки · Операция",
            String,
            string,
            (
                Property(
                    "operation",
                    "Операция",
                    "choice",
                    tuple(String.model_fields["operation"].annotation.__args__),
                ),
                value,
                Property("other", "Второе значение", "value", secret=True),
                Property("replacement", "Заменить на"),
                Property("separator", "Разделитель"),
                Property("start", "Начало substring", "integer"),
                Property("end", "Конец substring (пусто = до конца)", "optional_integer"),
            ),
        ),
        Action(
            "Logic.Switch",
            "Логика · Switch",
            Switch,
            switch,
            (value, Property("cases", "Варианты: JSON-массив, выходы C1…", "json")),
        ),
        Action(
            "Logic.Loop",
            "Логика · Цикл N раз",
            Loop,
            loop,
            (Property("count", "Повторений / {{имя}}", "integer"), index, limit),
        ),
        Action(
            "Logic.ForEach",
            "Логика · For Each",
            ForEach,
            loop,
            (
                Property("items", "JSON-список / {{имя}}", "value"),
                Property("item", "Переменная элемента"),
                index,
                limit,
            ),
        ),
        Action(
            "Logic.Delay",
            "Логика · Пауза",
            Delay,
            delay,
            (Property("milliseconds", "Миллисекунд / {{имя}}", "integer"),),
        ),
        Action("Logic.Break", "Логика · Break", Empty, break_loop),
        Action("Logic.Continue", "Логика · Continue", Empty, continue_loop),
        Action("End.Success", "Завершить успешно", Empty, no_op),
        Action("End.Failure", "Завершить с ошибкой", Empty, no_op),
    ):
        registry.register(action)
