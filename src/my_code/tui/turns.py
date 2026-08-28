"""Foreground and background turn event presentation."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from prompt_toolkit.buffer import Buffer
from rich.console import RenderableType

from my_code.chat.events import (
    AttachmentLoaded,
    MaxStepsReached,
    ReasoningCompleted,
    ReasoningDelta,
    ReasoningStarted,
    TextCompleted,
    TextDelta,
    TextStarted,
    TodoListUpdated,
    ToolFinished,
    ToolStarted,
    TurnEvent,
    TurnSucceeded,
)
from my_code.chat.service import ChatService
from my_code.features.todos.models import TodoItem
from my_code.tui.activity import ToolActivityGroup
from my_code.tui.theme import TuiTheme
from my_code.tui.widgets import (
    assistant_message,
    reasoning_message,
    system_message,
    todo_snapshot,
    tool_activity_message,
    user_message,
)


class TurnFlowMixin:
    """Event consumer mixed into the application lifecycle controller."""

    runtime: ChatService
    buffer: Buffer
    theme: TuiTheme
    _busy: bool
    _activity: str
    _stream_text: str
    _reasoning_parts: list[str]
    _todos: tuple[TodoItem, ...]
    _tool_activity: ToolActivityGroup | None

    async def _write(self, renderable: RenderableType, *, clear: bool = False) -> None:
        raise NotImplementedError

    def _invalidate(self) -> None:
        raise NotImplementedError

    def _refresh_status(self) -> None:
        raise NotImplementedError

    async def _run_turn(
        self,
        prompt: str,
        events: AsyncIterator[TurnEvent],
        *,
        user: bool,
    ) -> None:
        self._busy = True
        self._activity = "my-code is working…"
        self._stream_text = ""
        self._reasoning_parts = []
        self.buffer.cancel_completion()
        if user:
            await self._write(user_message(prompt, self.theme))
        completed = False
        try:
            async for event in events:
                if isinstance(event, AttachmentLoaded):
                    await self._flush_tool_activity()
                    await self._write(system_message(event.display))
                elif isinstance(event, TextStarted):
                    await self._flush_tool_activity()
                    self._stream_text = ""
                elif isinstance(event, TextDelta):
                    await self._flush_tool_activity()
                    self._stream_text += event.text
                elif isinstance(event, TextCompleted):
                    await self._flush_tool_activity()
                    await self._commit_assistant_text(event.text)
                elif isinstance(event, ReasoningStarted):
                    await self._flush_tool_activity()
                    self._reasoning_parts = []
                elif isinstance(event, ReasoningDelta):
                    await self._flush_tool_activity()
                    while len(self._reasoning_parts) <= event.part_index:
                        self._reasoning_parts.append("")
                    self._reasoning_parts[event.part_index] += event.text
                elif isinstance(event, ReasoningCompleted):
                    await self._flush_tool_activity()
                    await self._commit_reasoning(event)
                elif isinstance(event, ToolStarted):
                    if self._tool_activity is None:
                        self._tool_activity = ToolActivityGroup()
                    self._tool_activity.start(event.tool_use_id, event.presentation)
                    self._activity = event.presentation.activity
                elif isinstance(event, ToolFinished):
                    activity = self._tool_activity
                    item = (
                        activity.finish(
                            event.tool_use_id,
                            event.presentation,
                            is_error=event.is_error,
                        )
                        if activity is not None
                        else None
                    )
                    if (
                        activity is not None
                        and item is not None
                        and not event.is_error
                        and _is_todo(item.use)
                    ):
                        activity.remove(event.tool_use_id)
                    self._activity = "my-code is working…"
                elif isinstance(event, TodoListUpdated):
                    await self._flush_tool_activity()
                    self._todos = event.todos
                    await self._write(todo_snapshot(event.todos))
                elif isinstance(event, TurnSucceeded):
                    partial_text = self._retire_transient_content()
                    self._activity = ""
                    await self._flush_tool_activity()
                    if partial_text:
                        await self._write(assistant_message(partial_text))
                    await self._write(
                        system_message(
                            f"Done · {event.completed_steps} steps · "
                            f"{event.input_tokens} input / "
                            f"{event.output_tokens} output tokens"
                        )
                    )
                    completed = True
                elif isinstance(event, MaxStepsReached):
                    partial_text = self._retire_transient_content()
                    self._activity = ""
                    await self._flush_tool_activity()
                    if partial_text:
                        await self._write(assistant_message(partial_text))
                    await self._write(
                        system_message(
                            "Max steps reached "
                            f"({event.completed_steps}/{event.max_steps}) · "
                            f"{event.input_tokens} input / "
                            f"{event.output_tokens} output tokens",
                            error=True,
                        )
                    )
                    completed = True
                self._invalidate()
        except asyncio.CancelledError:
            partial_text = self._retire_transient_content()
            self._activity = ""
            await self._interrupt_and_flush_tools()
            await self._write(system_message("Turn interrupted.", error=True))
            if partial_text:
                await self._write(assistant_message(partial_text))
            raise
        except Exception as error:
            partial_text = self._retire_transient_content()
            self._activity = ""
            await self._interrupt_and_flush_tools()
            await self._write(system_message(f"Error: {error}", error=True))
            if partial_text:
                await self._write(assistant_message(partial_text))
        finally:
            partial_text = self._retire_transient_content()
            self._activity = ""
            await self._interrupt_and_flush_tools()
            if not completed and partial_text:
                await self._write(assistant_message(partial_text))
            self._busy = False
            self._refresh_status()

    async def _consume_background_event(self, event: TurnEvent) -> None:
        if isinstance(event, TextDelta):
            await self._flush_tool_activity()
            self._stream_text += event.text
        elif isinstance(event, TextCompleted):
            await self._flush_tool_activity()
            await self._commit_assistant_text(event.text)
        elif isinstance(event, ReasoningDelta):
            await self._flush_tool_activity()
            while len(self._reasoning_parts) <= event.part_index:
                self._reasoning_parts.append("")
            self._reasoning_parts[event.part_index] += event.text
        elif isinstance(event, ReasoningCompleted):
            await self._flush_tool_activity()
            await self._commit_reasoning(event)
        elif isinstance(event, ToolStarted):
            if self._tool_activity is None:
                self._tool_activity = ToolActivityGroup()
            self._tool_activity.start(event.tool_use_id, event.presentation)
            self._activity = event.presentation.activity
        elif isinstance(event, ToolFinished):
            activity = self._tool_activity
            item = (
                activity.finish(
                    event.tool_use_id,
                    event.presentation,
                    is_error=event.is_error,
                )
                if activity is not None
                else None
            )
            if (
                activity is not None
                and item is not None
                and not event.is_error
                and _is_todo(item.use)
            ):
                activity.remove(event.tool_use_id)
            self._activity = "Handling background task…"
        elif isinstance(event, TodoListUpdated):
            await self._flush_tool_activity()
            self._todos = event.todos
            await self._write(todo_snapshot(event.todos))
        elif isinstance(event, TurnSucceeded):
            partial_text = self._retire_transient_content()
            self._activity = ""
            await self._flush_tool_activity()
            if partial_text:
                await self._write(assistant_message(partial_text))
            await self._write(
                system_message(
                    f"Background done · {event.completed_steps} steps · "
                    f"{event.input_tokens} input / {event.output_tokens} output tokens"
                )
            )
        elif isinstance(event, MaxStepsReached):
            partial_text = self._retire_transient_content()
            self._activity = ""
            await self._flush_tool_activity()
            if partial_text:
                await self._write(assistant_message(partial_text))
            await self._write(
                system_message(
                    f"Background continuation reached max steps ({event.max_steps}).",
                    error=True,
                )
            )

    async def _commit_assistant_text(self, text: str) -> None:
        """Move assistant text out of the live region before writing scrollback."""

        self._stream_text = ""
        await self._write(assistant_message(text))

    async def _commit_reasoning(self, event: ReasoningCompleted) -> None:
        """Move reasoning out of the live region before writing scrollback."""

        self._reasoning_parts = []
        await self._write(reasoning_message(event.presentation))

    def _retire_transient_content(self) -> str:
        """Clear unfinished multi-line projections and return partial text."""

        partial_text = self._stream_text
        self._stream_text = ""
        self._reasoning_parts = []
        return partial_text

    async def _flush_tool_activity(self) -> None:
        activity = self._tool_activity
        if activity is None:
            return
        self._tool_activity = None
        if activity:
            await self._write(tool_activity_message(activity))

    async def _interrupt_and_flush_tools(self) -> None:
        if self._tool_activity is not None:
            self._tool_activity.interrupt_running()
        await self._flush_tool_activity()


def _is_todo(use: object) -> bool:
    display_name = getattr(use, "display_name", "")
    return display_name in {"TodoWrite", "Update Todos"}


__all__ = ["TurnFlowMixin"]
