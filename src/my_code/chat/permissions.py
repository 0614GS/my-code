"""Permission prompting bridge for interactive chat frontends."""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from my_code.foundation.json import JsonObject
from my_code.permissions.models import (
    PermissionConfirmation,
    PermissionPrompt,
    PermissionUpdate,
)
from my_code.tools.presentation import ToolUsePresentation


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
        self._pending: set[asyncio.Task[object]] = set()

    @property
    def pending_count(self) -> int:
        return len(self._pending)

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
        task = asyncio.current_task()
        if task is not None:
            self._pending.add(task)
        try:
            return await self._handler(
                PermissionRequest(
                    tool_name=request.tool_name,
                    tool_input=request.tool_input,
                    message=request.decision.message,
                    presentation=presentation,
                    suggestions=request.decision.suggestions,
                )
            )
        finally:
            if task is not None:
                self._pending.discard(task)

    async def close(self) -> None:
        current = asyncio.current_task()
        pending = tuple(task for task in self._pending if task is not current)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._handler = None


__all__ = [
    "DeferredPermissionPrompter",
    "PermissionHandler",
    "PermissionRequest",
]
