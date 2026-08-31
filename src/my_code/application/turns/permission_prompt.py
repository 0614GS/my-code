"""Permission prompting bridge for interactive frontends."""

import asyncio

from my_code.application.contracts.permissions import (
    PermissionHandler,
    PermissionRequest,
)
from my_code.permissions.models import PermissionConfirmation, PermissionPrompt
from my_code.tools.presentation import ToolUsePresentation, tool_display_category


class DeferredPermissionPrompter:
    """Route permission checks to the currently attached frontend."""

    def __init__(self) -> None:
        self._handler: PermissionHandler | None = None
        self._pending: set[asyncio.Task[object]] = set()
        self._prompt_lock = asyncio.Lock()

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
            category=tool_display_category(request.tool_name),
        )
        task = asyncio.current_task()
        if task is not None:
            self._pending.add(task)
        try:
            async with self._prompt_lock:
                handler = self._handler
                if handler is None:
                    return PermissionConfirmation(False)
                return await handler(
                    PermissionRequest(
                        tool_name=request.tool_name,
                        tool_input=request.tool_input,
                        message=request.decision.message,
                        presentation=presentation,
                        suggestions=request.decision.suggestions,
                        category=request.category,
                        requester=request.requester,
                        run_id=request.run_id,
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
]
