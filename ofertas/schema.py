"""Tiny JSON Schema validator for the subset we use (avoids the jsonschema dep).

Supports: type (object/array/string/integer/number/boolean), properties,
required, additionalProperties=false, enum, minimum, maximum, maxLength,
items, maxItems.
"""

from __future__ import annotations

from typing import Any

_TYPES = {
    "object": dict,
    "array": list,
    "string": str,
    "boolean": bool,
    "number": (int, float),
    "integer": int,
}


def validate(value: Any, schema: dict, path: str = "$") -> list[str]:
    errors: list[str] = []
    t = schema.get("type")
    if t:
        py = _TYPES[t]
        ok = isinstance(value, py) and not (t in ("integer", "number") and isinstance(value, bool))
        if not ok:
            return [f"{path}: esperado {t}, veio {type(value).__name__}"]
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: {value!r} fora de {schema['enum']}")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: {value} < {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: {value} > {schema['maximum']}")
    if isinstance(value, str) and "maxLength" in schema and len(value) > schema["maxLength"]:
        errors.append(f"{path}: texto maior que {schema['maxLength']}")
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for req in schema.get("required", []):
            if req not in value:
                errors.append(f"{path}: falta '{req}'")
        for k, v in value.items():
            if k in props:
                errors.extend(validate(v, props[k], f"{path}.{k}"))
            elif schema.get("additionalProperties") is False:
                errors.append(f"{path}: campo inesperado '{k}'")
    if isinstance(value, list):
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(f"{path}: mais de {schema['maxItems']} itens")
        if "items" in schema:
            for i, v in enumerate(value):
                errors.extend(validate(v, schema["items"], f"{path}[{i}]"))
    return errors
