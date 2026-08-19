"""Permission prompting bridge for interactive chat frontends."""

from nano_code.chat.models import PermissionHandler, PermissionRequest
from nano_code.permissions import PermissionConfirmation, PermissionPrompt
from nano_code.tools import ToolUsePresentation


class DeferredPermissionPrompter:
    """Route permission checks to the currently attached frontend."""

    def __init__(self) -> None:
        self._handler: PermissionHandler | None = None

    def set_handler(self, handler: PermissionHandler) -> None:
        self._handler = handler

    async def confirm(self, request: PermissionPrompt) -> PermissionConfirmation:
        if self._handler is None:
            return PermissionConfirmation(False)
        presentation = ToolUsePresentation(
            display_name=request.display_name,
            summary=request.summary,
            activity=request.activity,
        )
        return await self._handler(
            PermissionRequest(
                tool_name=request.tool_name,
                tool_input=request.tool_input,
                message=request.decision.message,
                presentation=presentation,
                suggestions=request.decision.suggestions,
            )
        )
