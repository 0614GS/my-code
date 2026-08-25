"""MCP schema validation rejects malformed definitions and invocation inputs."""

import pytest

from my_code.mcp.schema import validate_tool_input, validate_tool_schema
from my_code.tools.base import ToolInputError


def test_nested_supported_schema_validates_input() -> None:
    schema = validate_tool_schema(
        {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "properties": {"count": {"type": "integer", "minimum": 1}},
                        "required": ["count"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["items"],
            "additionalProperties": False,
        }
    )

    validate_tool_input(schema, {"items": [{"count": 2}]})

    with pytest.raises(ToolInputError, match="minimum"):
        validate_tool_input(schema, {"items": [{"count": 0}]})
    with pytest.raises(ToolInputError, match="unexpected property"):
        validate_tool_input(schema, {"items": [{"count": 2}], "extra": True})


@pytest.mark.parametrize(
    "schema, message",
    [
        ({"type": "string"}, "root type must be object"),
        ({"type": "object", "$ref": "#/$defs/input"}, "not supported"),
        ({"type": "object", "required": ["value", "value"]}, "duplicates"),
        (
            {"type": "object", "properties": {"value": {"type": "mystery"}}},
            "unsupported type",
        ),
    ],
)
def test_invalid_remote_schema_is_rejected(schema: object, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        validate_tool_schema(schema)
