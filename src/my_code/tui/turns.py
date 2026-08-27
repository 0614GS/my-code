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
from my_code.tools.presentation import ToolUsePresentation
from my_code.tui.theme import TuiTheme
from my_code.tui.widgets import (
    assistant_message,
    reasoning_message,
    system_message,
    tool_message,
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
    _background_tools: dict[str, ToolUsePresentation]

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
        tools: dict[str, ToolUsePresentation] = {}
        text_fixed = False
        completed = False
        try:
            async for event in events:
                if isinstance(event, AttachmentLoaded):
                    await self._write(system_message(event.display))
                elif isinstance(event, TextStarted):
                    self._stream_text = ""
                    text_fixed = False
                elif isinstance(event, TextDelta):
                    self._stream_text += event.text
                elif isinstance(event, TextCompleted):
                    await self._write(assistant_message(event.text))
                    self._stream_text = ""
                    text_fixed = True
                elif isinstance(event, ReasoningStarted):
                    self._reasoning_parts = []
                elif isinstance(event, ReasoningDelta):
                    while len(self._reasoning_parts) <= event.part_index:
                        self._reasoning_parts.append("")
                    self._reasoning_parts[event.part_index] += event.text
                elif isinstance(event, ReasoningCompleted):
                    await self._write(reasoning_message(event.presentation))
                    self._reasoning_parts = []
                elif isinstance(event, ToolStarted):
                    tools[event.tool_use_id] = event.presentation
                    self._activity = event.presentation.activity
                elif isinstance(event, ToolFinished):
                    use = tools.pop(event.tool_use_id, None)
                    if use is not None:
                        await self._write(
                            tool_message(
                                use, event.presentation, is_error=event.is_error
                            )
                        )
                    self._activity = "my-code is working…"
                elif isinstance(event, TodoListUpdated):
                    self._todos = event.todos
                elif isinstance(event, TurnSucceeded):
                    if self._stream_text and not text_fixed:
                        await self._write(assistant_message(self._stream_text))
                        self._stream_text = ""
                    await self._write(
                        system_message(
                            f"Done · {event.completed_steps} steps · "
                            f"{event.input_tokens} input / "
                            f"{event.output_tokens} output tokens"
                        )
                    )
                    completed = True
                elif isinstance(event, MaxStepsReached):
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
            await self._write(system_message("Turn interrupted.", error=True))
            raise
        except Exception as error:
            await self._write(system_message(f"Error: {error}", error=True))
        finally:
            if not completed and self._stream_text:
                await self._write(assistant_message(self._stream_text))
            for use in tools.values():
                await self._write(
                    system_message(f"Interrupted tool: {use.display_name}", error=True)
                )
            self._stream_text = ""
            self._reasoning_parts = []
            self._activity = ""
            self._busy = False
            self._refresh_status()

    async def _consume_background_event(self, event: TurnEvent) -> None:
        if isinstance(event, TextDelta):
            self._stream_text += event.text
        elif isinstance(event, TextCompleted):
            await self._write(assistant_message(event.text))
            self._stream_text = ""
        elif isinstance(event, ReasoningDelta):
            while len(self._reasoning_parts) <= event.part_index:
                self._reasoning_parts.append("")
            self._reasoning_parts[event.part_index] += event.text
        elif isinstance(event, ReasoningCompleted):
            await self._write(reasoning_message(event.presentation))
            self._reasoning_parts = []
        elif isinstance(event, ToolStarted):
            self._background_tools[event.tool_use_id] = event.presentation
            self._activity = event.presentation.activity
        elif isinstance(event, ToolFinished):
            use = self._background_tools.pop(event.tool_use_id, None)
            if use is not None:
                await self._write(
                    tool_message(use, event.presentation, is_error=event.is_error)
                )
            self._activity = "Handling background task…"
        elif isinstance(event, TodoListUpdated):
            self._todos = event.todos
        elif isinstance(event, TurnSucceeded):
            if self._stream_text:
                await self._write(assistant_message(self._stream_text))
                self._stream_text = ""
            await self._write(
                system_message(
                    f"Background done · {event.completed_steps} steps · "
                    f"{event.input_tokens} input / {event.output_tokens} output tokens"
                )
            )
        elif isinstance(event, MaxStepsReached):
            await self._write(
                system_message(
                    f"Background continuation reached max steps ({event.max_steps}).",
                    error=True,
                )
            )


__all__ = ["TurnFlowMixin"]
