"""A capability boundary that makes any Tool invocation read-only."""

from my_code.conversation.presentation import ToolResultPresentation
from my_code.foundation.json import JsonObject
from my_code.model.request import ModelToolDefinition
from my_code.permissions.models import (
    PermissionDecisionKind,
    PermissionDecisionReason,
    ToolPermissionContext,
    ToolPermissionResult,
)
from my_code.tools.base import (
    Tool,
    ToolContext,
    ToolExecutionError,
    ToolOutput,
)
from my_code.tools.presentation import ToolUsePresentation


class ReadOnlyToolProxy(Tool):
    """Delegate a Tool while denying every invocation it deems mutating."""

    def __init__(self, wrapped: Tool) -> None:
        self.wrapped = wrapped

    @property
    def definition(self) -> ModelToolDefinition:
        return self.wrapped.definition

    def is_concurrency_safe(self, tool_input: JsonObject) -> bool:
        return self.wrapped.is_concurrency_safe(tool_input)

    def user_facing_name(self, tool_input: JsonObject) -> str:
        return self.wrapped.user_facing_name(tool_input)

    def get_tool_use_summary(self, tool_input: JsonObject) -> str:
        return self.wrapped.get_tool_use_summary(tool_input)

    def get_activity_description(self, tool_input: JsonObject) -> str:
        return self.wrapped.get_activity_description(tool_input)

    def present_use(self, tool_input: JsonObject) -> ToolUsePresentation:
        return self.wrapped.present_use(tool_input)

    def present_result(
        self, tool_input: JsonObject, output: ToolOutput
    ) -> ToolResultPresentation:
        return self.wrapped.present_result(tool_input, output)

    def present_error(
        self, tool_input: JsonObject, message: str
    ) -> ToolResultPresentation:
        return self.wrapped.present_error(tool_input, message)

    def to_model_result(self, output: ToolOutput) -> str:
        return self.wrapped.to_model_result(output)

    def is_read_only(self, tool_input: JsonObject, context: ToolContext) -> bool:
        return self.wrapped.is_read_only(tool_input, context)

    def validate_input(self, tool_input: JsonObject) -> None:
        self.wrapped.validate_input(tool_input)

    async def check_permissions(
        self,
        tool_input: JsonObject,
        context: ToolPermissionContext,
    ) -> ToolPermissionResult:
        if not self.wrapped.is_read_only(
            tool_input,
            ToolContext(context.workspace_root),
        ):
            return ToolPermissionResult.deny(
                message="Explore agents may execute read-only tool calls only.",
                reason=PermissionDecisionReason(
                    PermissionDecisionKind.SAFETY,
                    "explore-read-only",
                ),
            )
        return await self.wrapped.check_permissions(tool_input, context)

    async def execute(
        self,
        tool_input: JsonObject,
        context: ToolContext,
    ) -> ToolOutput:
        if not self.wrapped.is_read_only(tool_input, context):
            raise ToolExecutionError(
                "Explore agents may execute read-only tool calls only."
            )
        return await self.wrapped.execute(tool_input, context)


__all__ = ["ReadOnlyToolProxy"]
