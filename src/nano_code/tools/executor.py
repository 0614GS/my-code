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
            # Unknown names are reported to the model as protocol results instead
            # of tearing down the whole agent loop.
            return self._error(call, f"Unknown tool: {call.name}")

        # Validation must precede permission checks: never ask a user to approve
        # malformed or semantically ambiguous input.
        try:
            tool.validate_input(call.input)
        except (ToolInputError, ValueError, TypeError) as error:
            return self._error(call, f"Invalid input: {error}")

        # Permission is a separate policy layer; Tool.execute is called only after
        # both static policy and any required human confirmation succeed.
        decision = await self.policy.decide(tool, call.input, self.context)
        if decision.behavior is PermissionBehavior.DENY:
            return self._error(call, f"Permission denied: {decision.message}")
        if decision.behavior is PermissionBehavior.ASK:
            permission_input = (
                call.input if decision.updated_input is None else decision.updated_input
            )
            confirmation = await self.prompter.confirm(tool, permission_input, decision)
            if not confirmation.allowed:
                feedback = (
                    f" User feedback: {confirmation.feedback}"
                    if confirmation.feedback is not None
                    else ""
                )
                return self._error(
                    call,
                    "Permission denied: approval was not provided. "
                    f"Reason: {decision.reason}.{feedback}",
                )

        try:
            # Tool-specific permission checks may normalize or constrain input;
            # execution must consume the exact input that was approved.
            approved_input = (
                call.input if decision.updated_input is None else decision.updated_input
            )
            output = await tool.execute(approved_input, self.context)

            # Externalize before constructing the API block so every later layer
            # sees the same bounded, replayable content.
            content = self.result_store.externalize(call.id, output.content)
            return ToolResultBlock(
                tool_use_id=call.id,
                content=content,
                is_error=output.is_error,
            )
        except (ToolInputError, ToolExecutionError, OSError, UnicodeError) as error:
            return self._error(call, f"{type(error).__name__}: {error}")
        except Exception as error:
            # Unexpected exception text may contain credentials or implementation
            # details. Preserve only the stable exception class for the model.
            return self._error(
                call, f"Unexpected {type(error).__name__} while executing {call.name}"
            )

    @staticmethod
    def _error(call: ToolUseBlock, message: str) -> ToolResultBlock:
        # Keeping the original ID is non-negotiable: providers reject histories
        # containing a tool_use without a matching tool_result.
        return ToolResultBlock(
            tool_use_id=call.id,
            content=message,
            is_error=True,
        )
