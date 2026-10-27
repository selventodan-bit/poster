"""Portable filesystem actions restricted by the host-owned FileSandbox."""

from fnmatch import fnmatchcase
from functools import partial
from pydantic import Field
from .actions import Action
from .action_fields import properties
from .model import DataModel, GraphError
from .resources import MAX_DATA


class PathParams(DataModel):
    path: str = Field(default="data.txt", description="Путь относительно рабочего каталога")


class Text(PathParams):
    encoding: str = Field(default="utf-8", description="Кодировка: utf-8, utf-8-sig, utf-16, cp1251…")


class Write(Text):
    text: str = ""
    overwrite: bool = False


class Transfer(DataModel):
    source: str = "input.txt"
    destination: str = "output.txt"
    overwrite: bool = False


class Delete(PathParams):
    missing_ok: bool = False


class Directory(PathParams):
    path: str = "."
    pattern: str = "*"
    include_hidden: bool = False


async def file_action(ctx, p, operation):
    files = ctx.files
    try:
        if operation == "ReadText":
            value = files.read(p.path, MAX_DATA).decode(p.encoding)
        elif operation in {"WriteText", "AppendText"}:
            text = p.text
            if operation == "AppendText" and files.path(p.path).exists():
                text = files.read(p.path).decode(p.encoding) + text
            value = files.write(
                p.path, text.encode(p.encoding), overwrite=p.overwrite or operation == "AppendText"
            )
        elif operation == "Exists":
            value = files.path(p.path, allow_root=True).exists()
        elif operation in {"Copy", "Move"}:
            source = files.path(p.source)
            dest = files.path(p.destination)
            if source == dest:
                raise GraphError("FILE_PATH_INVALID")
            import asyncio

            value = await asyncio.to_thread(files.copy, p.source, p.destination, overwrite=p.overwrite)
            if operation == "Move":
                source.unlink()
        elif operation == "Delete":
            path = files.path(p.path)
            if not path.exists() and not p.missing_ok:
                raise GraphError("FILE_MISSING")
            if path.is_dir():
                path.rmdir()
            else:
                path.unlink(missing_ok=p.missing_ok)
            value = True
        elif operation == "CreateDirectory":
            path = files.path(p.path)
            path.mkdir(parents=True, exist_ok=True)
            value = True
        else:
            root = files.path(p.path, allow_root=True)
            value = []
            for i, path in enumerate(root.iterdir()):
                if i >= 10000:
                    raise GraphError("FILE_LIMIT")
                if (p.include_hidden or not path.name.startswith(".")) and fnmatchcase(path.name, p.pattern):
                    safe = files.path(str(path))
                    value.append(
                        {
                            "name": safe.name,
                            "path": str(safe.relative_to(files.root))
                            if safe.is_relative_to(files.root)
                            else str(safe),
                            "directory": safe.is_dir(),
                            "bytes": safe.stat().st_size if safe.is_file() else None,
                        }
                    )
            value.sort(key=lambda item: item["name"])
        return "success", {"value": value}
    except (OSError, UnicodeError, LookupError) as exc:
        raise GraphError("FILE_OPERATION") from exc


def register_file_actions(registry):
    for name, label, model in [
        ("ReadText", "Читать текст", Text),
        ("WriteText", "Записать текст", Write),
        ("AppendText", "Дополнить текст", Write),
        ("Exists", "Проверить наличие", PathParams),
        ("Copy", "Копировать", Transfer),
        ("Move", "Переместить", Transfer),
        ("Delete", "Удалить", Delete),
        ("ListDirectory", "Список каталога", Directory),
        ("CreateDirectory", "Создать каталог", PathParams),
    ]:
        registry.register(
            Action(
                "Files." + name,
                "Файлы · " + label,
                model,
                partial(file_action, operation=name),
                properties(model, paths=("path", "source", "destination"), secrets=("text",)),
                reveal_parameters=True,
            )
        )
