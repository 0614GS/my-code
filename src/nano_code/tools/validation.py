"""工具实现共用的输入读取辅助函数。"""

from nano_code.model import JsonObject
from nano_code.tools.base import ToolInputError


def required_string(
    tool_input: JsonObject, key: str, *, allow_empty: bool = False
) -> str:
    value = tool_input.get(key)
    if not isinstance(value, str):
        raise ToolInputError(f"{key!r} must be a string")
    if not allow_empty and not value.strip():
        raise ToolInputError(f"{key!r} must not be empty")
    return value


def optional_string(tool_input: JsonObject, key: str, default: str) -> str:
    value = tool_input.get(key, default)
    if not isinstance(value, str):
        raise ToolInputError(f"{key!r} must be a string")
    return value


def optional_int(
    tool_input: JsonObject,
    key: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    value = tool_input.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ToolInputError(f"{key!r} must be an integer")
    if not minimum <= value <= maximum:
        raise ToolInputError(f"{key!r} must be between {minimum} and {maximum}")
    return value


def optional_bool(tool_input: JsonObject, key: str, default: bool) -> bool:
    value = tool_input.get(key, default)
    if not isinstance(value, bool):
        raise ToolInputError(f"{key!r} must be a boolean")
    return value
