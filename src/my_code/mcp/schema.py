"""Dependency-free validation for the MCP tool-schema subset exposed locally."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping

from my_code.model.primitives import JsonObject, JsonValue, to_json_object
from my_code.tools.base import ToolInputError

_JSON_TYPES = frozenset(
    {"null", "boolean", "object", "array", "number", "integer", "string"}
)


def validate_tool_schema(schema: object) -> JsonObject:
    """Validate and freeze-by-copy one remotely supplied input schema."""

    try:
        copied = to_json_object(schema)
    except (TypeError, ValueError) as error:
        raise ValueError("MCP tool inputSchema must be a JSON object") from error
    _validate_schema_shape(copied, path="$", root=True)
    return copied


def validate_tool_input(schema: JsonObject, value: JsonObject) -> None:
    """Validate invocation input before permission evaluation and remote I/O."""

    try:
        _validate_value(schema, value, path="$", root_schema=schema)
    except ValueError as error:
        raise ToolInputError(str(error)) from error


def _validate_schema_shape(schema: JsonObject, *, path: str, root: bool) -> None:
    if "$ref" in schema:
        raise ValueError(f"{path}.$ref is not supported in MCP M5a")
    declared = schema.get("type")
    types = _schema_types(declared, f"{path}.type")
    if root and (declared is None or "object" not in types):
        raise ValueError("MCP tool inputSchema root type must be object")

    properties = schema.get("properties")
    if properties is not None:
        if not isinstance(properties, dict) or any(
            not isinstance(key, str) for key in properties
        ):
            raise ValueError(f"{path}.properties must be an object")
        for name, child in properties.items():
            _validate_child_schema(child, path=f"{path}.properties.{name}")

    required = schema.get("required")
    if required is not None:
        if not isinstance(required, list) or any(
            not isinstance(item, str) or not item for item in required
        ):
            raise ValueError(f"{path}.required must be an array of names")
        if len(required) != len(set(required)):
            raise ValueError(f"{path}.required must not contain duplicates")

    additional = schema.get("additionalProperties")
    if additional is not None and not isinstance(additional, bool):
        _validate_child_schema(additional, path=f"{path}.additionalProperties")

    items = schema.get("items")
    if items is not None:
        _validate_child_schema(items, path=f"{path}.items")

    for keyword in ("allOf", "anyOf", "oneOf"):
        alternatives = schema.get(keyword)
        if alternatives is None:
            continue
        if not isinstance(alternatives, list) or not alternatives:
            raise ValueError(f"{path}.{keyword} must be a non-empty array")
        for index, child in enumerate(alternatives):
            _validate_child_schema(child, path=f"{path}.{keyword}[{index}]")
    if "not" in schema:
        _validate_child_schema(schema["not"], path=f"{path}.not")

    enum = schema.get("enum")
    if enum is not None and (not isinstance(enum, list) or not enum):
        raise ValueError(f"{path}.enum must be a non-empty array")
    pattern = schema.get("pattern")
    if pattern is not None:
        if not isinstance(pattern, str):
            raise ValueError(f"{path}.pattern must be a string")
        try:
            re.compile(pattern)
        except re.error as error:
            raise ValueError(f"{path}.pattern is invalid") from error
    for keyword in ("minLength", "maxLength", "minItems", "maxItems"):
        _nonnegative_integer(schema, keyword, path)
    for keyword in (
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
    ):
        number = schema.get(keyword)
        if number is not None and (
            isinstance(number, bool) or not isinstance(number, (int, float))
        ):
            raise ValueError(f"{path}.{keyword} must be a number")
    if schema.get("multipleOf") is not None and schema["multipleOf"] <= 0:  # type: ignore[operator]
        raise ValueError(f"{path}.multipleOf must be positive")


def _validate_child_schema(value: object, *, path: str) -> None:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError(f"{path} must be a schema object")
    _validate_schema_shape(to_json_object(value), path=path, root=False)


def _schema_types(value: object, path: str) -> frozenset[str]:
    if value is None:
        return _JSON_TYPES
    if isinstance(value, str):
        values = (value,)
    elif (
        isinstance(value, list)
        and value
        and all(isinstance(item, str) for item in value)
    ):
        values = tuple(value)
    else:
        raise ValueError(f"{path} must be a JSON Schema type or non-empty type array")
    unknown = set(values) - _JSON_TYPES
    if unknown:
        raise ValueError(f"{path} contains unsupported type {sorted(unknown)[0]!r}")
    return frozenset(values)


def _nonnegative_integer(schema: JsonObject, keyword: str, path: str) -> None:
    value = schema.get(keyword)
    if value is not None and (
        isinstance(value, bool) or not isinstance(value, int) or value < 0
    ):
        raise ValueError(f"{path}.{keyword} must be a non-negative integer")


def _validate_value(
    schema: JsonObject,
    value: JsonValue,
    *,
    path: str,
    root_schema: JsonObject,
) -> None:
    del root_schema  # Reserved for local-reference support in a later protocol driver.
    if "const" in schema and value != schema["const"]:
        raise ValueError(f"{path} must equal the schema const value")
    enum = schema.get("enum")
    if isinstance(enum, list) and value not in enum:
        raise ValueError(f"{path} must be one of the schema enum values")

    for keyword in ("allOf",):
        alternatives = schema.get(keyword)
        if isinstance(alternatives, list):
            for child in alternatives:
                _validate_value(
                    to_json_object(child),
                    value,
                    path=path,
                    root_schema=schema,
                )
    alternatives = schema.get("anyOf")
    if isinstance(alternatives, list) and not any(
        _matches(to_json_object(child), value, path) for child in alternatives
    ):
        raise ValueError(f"{path} does not match anyOf")
    alternatives = schema.get("oneOf")
    if (
        isinstance(alternatives, list)
        and sum(_matches(to_json_object(child), value, path) for child in alternatives)
        != 1
    ):
        raise ValueError(f"{path} must match exactly one oneOf schema")
    excluded = schema.get("not")
    if isinstance(excluded, dict) and _matches(to_json_object(excluded), value, path):
        raise ValueError(f"{path} matches a forbidden schema")

    types = _schema_types(schema.get("type"), f"{path}.type")
    if not any(_is_type(value, item) for item in types):
        expected = ", ".join(sorted(types))
        raise ValueError(f"{path} must have type {expected}")

    if isinstance(value, dict):
        _validate_object(schema, value, path)
    elif isinstance(value, list):
        _validate_array(schema, value, path)
    elif isinstance(value, str):
        _validate_string(schema, value, path)
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        _validate_number(schema, value, path)


def _matches(schema: JsonObject, value: JsonValue, path: str) -> bool:
    try:
        _validate_value(schema, value, path=path, root_schema=schema)
    except ValueError:
        return False
    return True


def _is_type(value: JsonValue, schema_type: str) -> bool:
    match schema_type:
        case "null":
            return value is None
        case "boolean":
            return isinstance(value, bool)
        case "object":
            return isinstance(value, dict)
        case "array":
            return isinstance(value, list)
        case "number":
            return isinstance(value, (int, float)) and not isinstance(value, bool)
        case "integer":
            return (
                isinstance(value, int)
                and not isinstance(value, bool)
                or isinstance(value, float)
                and value.is_integer()
            )
        case "string":
            return isinstance(value, str)
    return False


def _validate_object(schema: JsonObject, value: JsonObject, path: str) -> None:
    required = schema.get("required")
    if isinstance(required, list):
        missing = [name for name in required if name not in value]
        if missing:
            raise ValueError(f"{path} is missing required property {missing[0]!r}")
    raw_properties = schema.get("properties")
    properties: Mapping[str, object] = (
        raw_properties if isinstance(raw_properties, dict) else {}
    )
    additional = schema.get("additionalProperties", True)
    for name, item in value.items():
        child = properties.get(name)
        if isinstance(child, dict):
            _validate_value(
                to_json_object(child),
                item,
                path=f"{path}.{name}",
                root_schema=schema,
            )
        elif additional is False:
            raise ValueError(f"{path} contains unexpected property {name!r}")
        elif isinstance(additional, dict):
            _validate_value(
                to_json_object(additional),
                item,
                path=f"{path}.{name}",
                root_schema=schema,
            )


def _validate_array(schema: JsonObject, value: list[JsonValue], path: str) -> None:
    minimum = schema.get("minItems")
    maximum = schema.get("maxItems")
    if isinstance(minimum, int) and len(value) < minimum:
        raise ValueError(f"{path} must contain at least {minimum} items")
    if isinstance(maximum, int) and len(value) > maximum:
        raise ValueError(f"{path} must contain at most {maximum} items")
    if schema.get("uniqueItems") is True:
        for index, item in enumerate(value):
            if item in value[:index]:
                raise ValueError(f"{path} must contain unique items")
    items = schema.get("items")
    if isinstance(items, dict):
        child = to_json_object(items)
        for index, item in enumerate(value):
            _validate_value(child, item, path=f"{path}[{index}]", root_schema=schema)


def _validate_string(schema: JsonObject, value: str, path: str) -> None:
    minimum = schema.get("minLength")
    maximum = schema.get("maxLength")
    if isinstance(minimum, int) and len(value) < minimum:
        raise ValueError(f"{path} must contain at least {minimum} characters")
    if isinstance(maximum, int) and len(value) > maximum:
        raise ValueError(f"{path} must contain at most {maximum} characters")
    pattern = schema.get("pattern")
    if isinstance(pattern, str) and re.search(pattern, value) is None:
        raise ValueError(f"{path} does not match the required pattern")


def _validate_number(schema: JsonObject, value: int | float, path: str) -> None:
    checks = {
        "minimum": lambda boundary: value >= boundary,
        "maximum": lambda boundary: value <= boundary,
        "exclusiveMinimum": lambda boundary: value > boundary,
        "exclusiveMaximum": lambda boundary: value < boundary,
    }
    for keyword, compare in checks.items():
        boundary = schema.get(keyword)
        if isinstance(boundary, (int, float)) and not isinstance(boundary, bool):
            if not compare(boundary):
                raise ValueError(f"{path} violates {keyword}")
    multiple = schema.get("multipleOf")
    if isinstance(multiple, (int, float)) and not isinstance(multiple, bool):
        quotient = value / multiple
        if not math.isclose(quotient, round(quotient), rel_tol=1e-9, abs_tol=1e-9):
            raise ValueError(f"{path} must be a multiple of {multiple}")


__all__ = [
    "validate_tool_input",
    "validate_tool_schema",
]
