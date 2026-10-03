"""校验远端 MCP tool 的 JSON Schema 与每次调用输入。"""

from __future__ import annotations

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError

from my_code.foundation.json import JsonObject, to_json_object
from my_code.tools.base import ToolInputError


def validate_tool_schema(schema: object) -> JsonObject:
    """只接受本地引用，避免校验远端定义时访问任意网络地址。"""

    try:
        copied = to_json_object(schema)
    except (TypeError, ValueError) as error:
        raise ValueError("MCP tool inputSchema must be a JSON object") from error
    if copied.get("type") != "object":
        raise ValueError("MCP tool inputSchema root type must be object")
    _reject_external_refs(copied)
    try:
        Draft202012Validator.check_schema(copied)
    except SchemaError as error:
        raise ValueError("MCP tool inputSchema is invalid") from error
    return copied


def validate_tool_input(schema: JsonObject, value: JsonObject) -> None:
    try:
        Draft202012Validator(schema).validate(value)
    except ValidationError as error:
        raise ToolInputError(error.message) from error


def _reject_external_refs(value: object) -> None:
    if isinstance(value, dict):
        reference = value.get("$ref")
        if reference is not None and (
            not isinstance(reference, str) or not reference.startswith("#/")
        ):
            raise ValueError("External MCP schema references are not supported")
        for nested in value.values():
            _reject_external_refs(nested)
    elif isinstance(value, list):
        for nested in value:
            _reject_external_refs(nested)


__all__ = ["validate_tool_input", "validate_tool_schema"]
