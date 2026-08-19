"""TodoWrite tool owned and exported by the Todo feature."""

from my_code.features.todos.codec import TODO_WRITE_TOOL_NAME, parse_todo_input
from my_code.model.primitives import JsonObject
from my_code.model.request import ModelToolDefinition
from my_code.permissions.models import (
    PermissionDecisionKind,
    PermissionDecisionReason,
    ToolPermissionContext,
    ToolPermissionResult,
)
from my_code.tools.base import Tool, ToolContext, ToolOutput
from my_code.tools.presentation import ToolResultPresentation

_DESCRIPTION = """Update the structured todo list for the current coding session.
Use it for complex multi-step work, explicit user task lists, and newly discovered
follow-up work; skip it for a single trivial or purely informational request.
Keep task statuses current. Use pending, in_progress, or completed; normally keep
exactly one task in_progress. Each item must include imperative content and a
present-continuous activeForm. Replace the entire list on every call and remove
items that are no longer relevant."""


class TodoWriteTool(Tool):
    @property
    def definition(self) -> ModelToolDefinition:
        return ModelToolDefinition(
            name=TODO_WRITE_TOOL_NAME,
            description=_DESCRIPTION,
            input_schema={
                "type": "object",
                "properties": {
                    "todos": {
                        "type": "array",
                        "description": "The complete updated todo list",
                        "items": {
                            "type": "object",
                            "properties": {
                                "content": {
                                    "type": "string",
                                    "minLength": 1,
                                    "description": "Imperative task description",
                                },
                                "status": {
                                    "type": "string",
                                    "enum": [
                                        "pending",
                                        "in_progress",
                                        "completed",
                                    ],
                                },
                                "activeForm": {
                                    "type": "string",
                                    "minLength": 1,
                                    "description": "Present-continuous task label",
                                },
                            },
                            "required": ["content", "status", "activeForm"],
                            "additionalProperties": False,
                        },
                    }
                },
                "required": ["todos"],
                "additionalProperties": False,
            },
        )

    def user_facing_name(self, tool_input: JsonObject) -> str:
        del tool_input
        return "Update Todos"

    def get_tool_use_summary(self, tool_input: JsonObject) -> str:
        return f"{len(parse_todo_input(tool_input))} items"

    def get_activity_description(self, tool_input: JsonObject) -> str:
        del tool_input
        return "Updating todos"

    def is_read_only(self, tool_input: JsonObject, context: ToolContext) -> bool:
        # Todo 只投影已持久化的 tool call，不修改工作区或外部系统。
        del tool_input, context
        return True

    async def check_permissions(
        self, tool_input: JsonObject, context: ToolPermissionContext
    ) -> ToolPermissionResult:
        del context
        return ToolPermissionResult.allow(
            tool_input,
            message="Session todo state is allowed.",
            reason=PermissionDecisionReason(
                PermissionDecisionKind.TOOL, "session-state"
            ),
        )

    def present_result(
        self, tool_input: JsonObject, output: ToolOutput
    ) -> ToolResultPresentation:
        del output
        return ToolResultPresentation(
            summary=f"Updated {len(parse_todo_input(tool_input))} todo(s)"
        )

    def validate_input(self, tool_input: JsonObject) -> None:
        parse_todo_input(tool_input)

    async def execute(self, tool_input: JsonObject, context: ToolContext) -> ToolOutput:
        del context
        todos = parse_todo_input(tool_input)
        return ToolOutput(
            content=(
                "Todos have been modified successfully. Ensure that you continue "
                "to use the todo list to track your progress. Please proceed with "
                "the current tasks if applicable"
            ),
            metadata={"todo_count": len(todos)},
        )


__all__ = [
    "TodoWriteTool",
]
