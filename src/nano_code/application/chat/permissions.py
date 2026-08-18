"""Permission prompting bridge for interactive chat frontends."""

from nano_code.application.chat.contracts import PermissionHandler, PermissionRequest
from nano_code.application.chat.presentation import generic_tool_use_presentation
from nano_code.conversation import JsonObject
from nano_code.permissions import PermissionConfirmation
from nano_code.permissions.models import PermissionDecision
from nano_code.tools import Tool


class DeferredPermissionPrompter:
    """Route permission checks to the currently attached frontend."""

    def __init__(self) -> None:
        self._handler: PermissionHandler | None = None

    def set_handler(self, handler: PermissionHandler) -> None:
        self._handler = handler

    async def confirm(
        self, tool: Tool, tool_input: JsonObject, decision: PermissionDecision
    ) -> PermissionConfirmation:
        if self._handler is None:
            return PermissionConfirmation(False)
        try:
            presentation = tool.present_use(tool_input)
        except Exception:
            presentation = generic_tool_use_presentation(
                tool.definition.name, tool_input
            )
        return await self._handler(
            PermissionRequest(
                tool_name=tool.definition.name,
                tool_input=tool_input,
                message=decision.message,
                presentation=presentation,
                suggestions=decision.suggestions,
            )
        )
