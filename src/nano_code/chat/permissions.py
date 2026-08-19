"""Permission prompting bridge for interactive chat frontends."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from nano_code.model.primitives import JsonObject
from nano_code.permissions.models import (
    PermissionConfirmation,
    PermissionPrompt,
    PermissionUpdate,
)
from nano_code.tools.presentation import ToolUsePresentation


@dataclass(frozen=True, slots=True)
class PermissionRequest:
    tool_name: str
    tool_input: JsonObject
    message: str
    presentation: ToolUsePresentation
    suggestions: tuple[PermissionUpdate, ...] = ()


type PermissionHandler = Callable[
    [PermissionRequest], Awaitable[PermissionConfirmation]
]


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


__all__ = [
    "DeferredPermissionPrompter",
    "PermissionHandler",
    "PermissionRequest",
]
