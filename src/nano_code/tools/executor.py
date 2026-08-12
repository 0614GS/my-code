"""The uniform validation → permission → execution pipeline."""

from nano_code.messages import ToolResultBlock, ToolUseBlock
from nano_code.permissions import PermissionBehavior, PermissionPolicy
from nano_code.permissions.prompt import PermissionPrompter
from nano_code.tools.base import (
    ToolContext,
    ToolExecutionError,
    ToolInputError,
)
from nano_code.tools.registry import ToolRegistry
from nano_code.tools.result_store import ToolResultStore


class ToolExecutor:
    """Execute calls while preserving a tool-result for every normal failure."""

    def __init__(
        self,
        registry: ToolRegistry,
        policy: PermissionPolicy,
        prompter: PermissionPrompter,
        context: ToolContext,
        result_store: ToolResultStore,
    ) -> None:
        self.registry = registry
        self.policy = policy
        self.prompter = prompter
        self.context = context
        self.result_store = result_store

    async def execute(self, call: ToolUseBlock) -> ToolResultBlock:
        tool = self.registry.get(call.name)
        if tool is None:
            return self._error(call, f"Unknown tool: {call.name}")

        try:
            tool.validate_input(call.input)
        except (ToolInputError, ValueError, TypeError) as error:
            return self._error(call, f"Invalid input: {error}")

        decision = self.policy.decide(tool)
        if decision.behavior is PermissionBehavior.DENY:
            return self._error(call, f"Permission denied: {decision.message}")
        if decision.behavior is PermissionBehavior.ASK:
            approved = await self.prompter.confirm(tool, call.input, decision)
            if not approved:
                return self._error(
                    call,
                    "Permission denied: approval was not provided. "
                    f"Reason: {decision.reason}",
                )

        try:
            output = await tool.execute(call.input, self.context)
            content = self.result_store.externalize(call.id, output.content)
            return ToolResultBlock(
                tool_use_id=call.id,
                content=content,
                is_error=output.is_error,
            )
        except (ToolInputError, ToolExecutionError, OSError, UnicodeError) as error:
            return self._error(call, f"{type(error).__name__}: {error}")
        except Exception as error:
            return self._error(
                call, f"Unexpected {type(error).__name__} while executing {call.name}"
            )

    @staticmethod
    def _error(call: ToolUseBlock, message: str) -> ToolResultBlock:
        return ToolResultBlock(
            tool_use_id=call.id,
            content=message,
            is_error=True,
        )
