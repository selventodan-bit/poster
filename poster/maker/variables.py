"""Per-run JSON values, explicit references and taint-aware interpolation; no eval."""

from copy import deepcopy
import json
import re

from .model import GraphError, secret_refs

NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,79}$")
TEMPLATE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]{0,79})\s*\}\}")


def has_reference(value):
    if isinstance(value, str):
        return bool(TEMPLATE.search(value))
    if isinstance(value, dict):
        return set(value) == {"variable"} or any(has_reference(v) for v in value.values())
    return isinstance(value, list) and any(has_reference(v) for v in value)


def draft_parameters(spec, values):
    """Validate static fields now and dynamic fields with their declared defaults."""
    defaults = spec.params.model_construct().model_dump(mode="json")
    check = {**defaults, **values}
    for key, value in values.items():
        if has_reference(value) or (spec.reveal_parameters and secret_refs(value)):
            check[key] = defaults.get(key)
    try:
        spec.params.model_validate(check)
    except ValueError as exc:
        raise GraphError("PARAMETERS_INVALID") from exc
    return {**defaults, **values}


class Variables:
    def __init__(self, definitions, vault, inputs=None):
        self.vault = vault
        self.data = {}
        self.protected = {name for name, definition in definitions.items() if definition.secret}
        inputs = inputs or {}
        if any(name not in definitions or not definitions[name].input for name in inputs):
            raise GraphError("INPUT_UNKNOWN")
        for name, definition in definitions.items():
            value = inputs.get(name, deepcopy(definition.value))
            if definition.required and definition.input and value is None:
                raise GraphError("INPUT_MISSING")
            if definition.secret and value is not None and not secret_refs(value):
                value = self.vault.put_value(value)
            self.set(name, value)

    def set(self, name, value):
        if not NAME.fullmatch(name):
            raise GraphError("VARIABLE_NAME_INVALID")
        if name in self.protected and value is not None and not secret_refs(value):
            value = self.vault.put_value(value)
        try:
            raw = json.dumps(value, ensure_ascii=False, allow_nan=False)
        except (ValueError, TypeError) as exc:
            raise GraphError("VARIABLE_TYPE_INVALID") from exc
        if len(raw.encode("utf-8")) > 1_000_000 or (name not in self.data and len(self.data) >= 1000):
            raise GraphError("VARIABLE_LIMIT")
        self.data[name] = deepcopy(value)
        return value

    def get(self, name):
        if name not in self.data:
            raise GraphError("VARIABLE_MISSING")
        return deepcopy(self.data[name])

    def reveal(self, value, _refs=frozenset(), _depth=0):
        if _depth > 64:
            raise GraphError("VARIABLE_TYPE_INVALID")
        if isinstance(value, dict):
            if set(value) == {"secret_ref"}:
                ref = value["secret_ref"]
                if ref in _refs:
                    raise GraphError("SECRET_MISSING")
                return self.reveal(self.vault.get(ref), _refs | {ref}, _depth + 1)
            return {key: self.reveal(v, _refs, _depth + 1) for key, v in value.items()}
        if isinstance(value, list):
            return [self.reveal(v, _refs, _depth + 1) for v in value]
        return value

    def resolve(self, value):
        if isinstance(value, dict):
            if set(value) == {"variable"}:
                return self.get(value["variable"])
            return {key: self.resolve(v) for key, v in value.items()}
        if isinstance(value, list):
            return [self.resolve(v) for v in value]
        if not isinstance(value, str):
            return value
        exact = TEMPLATE.fullmatch(value)
        if exact:
            return self.get(exact[1])
        protected = False

        def replace(match):
            nonlocal protected
            result = self.get(match[1])
            protected |= bool(secret_refs(result))
            result = self.reveal(result)
            return result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)

        result = TEMPLATE.sub(replace, value)
        return self.vault.put(result) if protected else result

    def public(self, value):
        if isinstance(value, dict):
            if set(value) == {"secret_ref"}:
                return "••••"
            return {key: self.public(v) for key, v in value.items()}
        if isinstance(value, list):
            return [self.public(v) for v in value]
        return deepcopy(value)

    def snapshot(self):
        return self.public(self.data)
