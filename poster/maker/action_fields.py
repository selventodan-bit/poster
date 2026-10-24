"""Registry property generation with explicit UI overrides for data actions."""

from typing import get_args, get_origin, Literal

from .actions import Property


def properties(model, labels=None, *, paths=(), secrets=()):
    result = []
    for name, field in model.model_fields.items():
        annotation = field.annotation
        choices = get_args(annotation) if get_origin(annotation) is Literal else ()
        kind = (
            "choice"
            if choices
            else "bool"
            if annotation is bool
            else "integer"
            if annotation is int
            else "file"
            if name in paths
            else "text"
            if annotation is str
            else "value"
        )
        result.append(
            Property(
                name,
                (labels or {}).get(name, field.description or name),
                kind,
                choices,
                name in secrets,
                field.description or "",
            )
        )
    return tuple(result)
