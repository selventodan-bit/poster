"""Functional JSON Pointer and typed JSON list operations, shared by all UIs."""

from copy import deepcopy
import json
import random
import re
from typing import Any
from pydantic import Field
from .actions import Action
from .action_fields import properties
from .model import DataModel, GraphError
from .resources import MAX_DATA


def bounded(value):
    try:
        raw = json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (ValueError, TypeError, RecursionError) as exc:
        raise GraphError("JSON_INVALID") from exc
    if len(raw.encode("utf-8")) > MAX_DATA:
        raise GraphError("DATA_LIMIT")
    return value


def tokens(path):
    if path == "":
        return []
    if not path.startswith("/") or re.search(r"~(?![01])", path):
        raise GraphError("JSON_PATH_INVALID")
    return [p.replace("~1", "/").replace("~0", "~") for p in path[1:].split("/")]


def at(value, key):
    if isinstance(value, list):
        if not re.fullmatch(r"0|[1-9][0-9]*", key):
            raise GraphError("JSON_PATH_INVALID")
        key = int(key)
    elif not isinstance(value, dict):
        raise GraphError("JSON_PATH_MISSING")
    try:
        return value[key]
    except (KeyError, IndexError, TypeError) as exc:
        raise GraphError("JSON_PATH_MISSING") from exc


class Parse(DataModel):
    text: str = Field(default="null", description="Текст JSON или {{переменная}}")


class Stringify(DataModel):
    value: Any = None
    pretty: bool = False


class Pointer(DataModel):
    value: Any = Field(default_factory=dict, description="Исходный JSON / {{переменная}}")
    path: str = Field(default="", description="JSON Pointer: /items/0/name; пусто = весь документ")


class Set(Pointer):
    new_value: Any = None


class Merge(DataModel):
    left: dict[str, Any] = Field(default_factory=dict)
    right: dict[str, Any] = Field(default_factory=dict)
    deep: bool = True


async def json_action(ctx, p, operation):
    try:
        if operation == "Parse":
            value = json.loads(p.text)
        elif operation == "Stringify":
            value = json.dumps(
                bounded(p.value), ensure_ascii=False, allow_nan=False, indent=2 if p.pretty else None
            )
        elif operation == "Merge":

            def merge(a, b):
                out = deepcopy(a)
                for k, v in b.items():
                    out[k] = (
                        merge(out[k], v)
                        if p.deep and isinstance(out.get(k), dict) and isinstance(v, dict)
                        else deepcopy(v)
                    )
                return out

            value = merge(p.left, p.right)
        else:
            value = deepcopy(p.value)
            parts = tokens(p.path)
            if operation == "Get":
                for part in parts:
                    value = at(value, part)
            elif not parts:
                value = p.new_value if operation == "Set" else None
            else:
                parent = value
                for part in parts[:-1]:
                    parent = at(parent, part)
                key = parts[-1]
                if isinstance(parent, list):
                    if key == "-" and operation == "Set":
                        parent.append(p.new_value)
                    else:
                        at(parent, key)
                        index = int(key)
                        if operation == "Set":
                            parent[index] = p.new_value
                        else:
                            parent.pop(index)
                elif isinstance(parent, dict):
                    if operation == "Set":
                        parent[key] = p.new_value
                    else:
                        at(parent, key)
                        del parent[key]
                else:
                    raise GraphError("JSON_PATH_MISSING")
        return "success", {"value": bounded(value)}
    except (ValueError, TypeError, RecursionError) as exc:
        if isinstance(exc, GraphError):
            raise
        raise GraphError("JSON_INVALID") from exc


class List(DataModel):
    items: list[Any] = Field(
        default_factory=list, max_length=10000, description="JSON-массив / {{переменная}}"
    )


class Item(List):
    item: Any = None


class Index(List):
    index: int = Field(default=0, ge=0, le=10000, strict=True, description="Индекс с нуля")


class IndexedItem(Index):
    item: Any = None


class Shuffle(List):
    seed: int | None = Field(
        default=None, description="Seed для повторяемого перемешивания, null = случайный"
    )


def key(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


async def list_action(ctx, p, operation):
    items = deepcopy(p.items)
    value = items
    if operation == "Add":
        items.append(p.item)
    elif operation == "Insert":
        if p.index > len(items):
            raise GraphError("LIST_INDEX")
        items.insert(p.index, p.item)
    elif operation in {"Get", "Set", "Remove"}:
        if p.index >= len(items):
            raise GraphError("LIST_INDEX")
        if operation == "Get":
            value = items[p.index]
        elif operation == "Set":
            items[p.index] = p.item
        else:
            items.pop(p.index)
    elif operation == "Count":
        value = len(items)
    elif operation == "Contains":
        value = key(p.item) in {key(i) for i in items}
    elif operation == "Clear":
        value = []
    elif operation == "Unique":
        seen = set()
        value = []
        for item in items:
            if key(item) not in seen:
                seen.add(key(item))
                value.append(item)
    elif operation == "Shuffle":
        random.Random(p.seed).shuffle(items)
    if isinstance(value, list) and len(value) > 10000:
        raise GraphError("DATA_LIMIT")
    return "success", {"value": bounded(value)}


def register_data_actions(registry):
    from functools import partial

    for name, label, model in [
        ("Parse", "Разобрать", Parse),
        ("Stringify", "В текст", Stringify),
        ("Get", "Получить по пути", Pointer),
        ("Set", "Изменить", Set),
        ("Delete", "Удалить по пути", Pointer),
        ("Merge", "Объединить", Merge),
    ]:
        registry.register(
            Action(
                "JSON." + name,
                "JSON · " + label,
                model,
                partial(json_action, operation=name),
                properties(model),
                reveal_parameters=True,
            )
        )
    for name, label, model in [
        ("Create", "Создать", List),
        ("Add", "Добавить", Item),
        ("Insert", "Вставить", IndexedItem),
        ("Get", "Получить", Index),
        ("Set", "Изменить", IndexedItem),
        ("Remove", "Удалить", Index),
        ("Count", "Количество", List),
        ("Contains", "Содержит", Item),
        ("Clear", "Очистить", List),
        ("Unique", "Уникальные", List),
        ("Shuffle", "Перемешать", Shuffle),
    ]:
        registry.register(
            Action(
                "Lists." + name,
                "Списки · " + label,
                model,
                partial(list_action, operation=name),
                properties(model),
                reveal_parameters=True,
            )
        )
