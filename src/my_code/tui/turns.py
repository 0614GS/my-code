"""Foreground and background turn event presentation."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any

from prompt_toolkit.buffer import Buffer
from rich.console import RenderableType

from my_code.application.contracts.events import (
    AttachmentLoaded,
    CompactionCompleted,
    CompactionStarted,
    ContextUpdated,
    MaxStepsReached,
    ModelRequestPrepared,
    ModelStepCompleted,
    PlanCompleted,
    PlanDelta,
    PlanStarted,
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
    TurnInputAccepted,
    TurnInputFailed,
    TurnSucceeded,
)
from my_code.application.contracts.status import ApplicationStatus, ContextUsageView
from my_code.application.service import ApplicationService
from my_code.features.todos.models import TodoItem
from my_code.model.display import DisplayDensity
from my_code.tui.activity import ToolActivityGroup
from my_code.tui.block_flow import TurnBlockCoordinator
from my_code.tui.presentation import (
    compaction_activity_label,
    compaction_completed_message,
)
from my_code.tui.theme import TuiTheme
from my_code.tui.widgets import (
    assistant_message,
    block_separator,
    detailed_tool_call_message,
    injected_context_message,
    system_message,
    todo_snapshot,
    tool_activity_message,
    user_message,
)


class TurnFlowMixin:
    """Event consumer mixed into the application lifecycle controller."""

    application: ApplicationService
    buffer: Buffer
    theme: TuiTheme
    _busy: bool
    _agent_active: bool
    _stream_text: str
    _stream_answer_started: bool
    _stream_plan: str
    _reasoning_parts: list[str]
    _todos: tuple[TodoItem, ...]
    _tool_activity: ToolActivityGroup | None
    _blocks: TurnBlockCoordinator
    _context_status: ContextUsageView | None
    _status: ApplicationStatus | None
    _display_density: DisplayDensity
    _panel: str | None
    _panel_picker: Any
    _pending_plan: str | None

    async def _write(self, renderable: RenderableType, *, clear: bool = False) -> None:
        raise NotImplementedError

    async def _write_many(self, renderables: tuple[RenderableType, ...]) -> None:
        raise NotImplementedError

    def _update_stream_projection(self) -> tuple[str, ...]:
        raise NotImplementedError

    def _flush_stream_projection(self) -> tuple[str, ...]:
        raise NotImplementedError

    async def _write_stream_fragment(self, fragment: str, *, first: bool) -> None:
        raise NotImplementedError

    def _reset_stream_projection(self) -> None:
        raise NotImplementedError

    def _invalidate(self) -> None:
        raise NotImplementedError

    def _invalidate_streaming(self) -> None:
        raise NotImplementedError

    def _prepare_stream_frame(self, *, structural: bool = False) -> None:
        raise NotImplementedError

    def _invalidate_for_event(self, event: TurnEvent) -> None:
        """Redraw immediately for structural events, throttle streaming deltas."""

        if isinstance(event, (TextDelta, ReasoningDelta)):
            self._invalidate_streaming()
        else:
            self._prepare_stream_frame(structural=True)
            self._invalidate()

    def _refresh_status(self) -> None:
        raise NotImplementedError

    def _begin_agent_activity(self, label: str) -> None:
        raise NotImplementedError

    def _update_agent_activity(self, label: str) -> None:
        raise NotImplementedError

    def _end_agent_activity(self) -> None:
        raise NotImplementedError

    async def _run_turn(
        self,
        prompt: str,
        events: AsyncIterator[TurnEvent],
        *,
        user: bool,
    ) -> None:
        self._agent_active = True
        self._busy = True
        self._begin_agent_activity("my-code is working…")
        self._stream_text = ""
        self._stream_answer_started = False
        self._reset_stream_projection()
        self._reasoning_parts = []
        self._blocks.reset_group()
        self.buffer.cancel_completion()
        if user:
            if self._display_density.includes(DisplayDensity.DETAILED):
                await self._write(block_separator("User input"))
            await self._write(user_message(prompt, self.theme))
        try:
            async for event in events:
                if isinstance(event, TurnInputAccepted):
                    self._blocks.reset_group()
                    if self._display_density.includes(DisplayDensity.DETAILED):
                        await self._write(block_separator("User input"))
                    await self._write(user_message(event.prompt, self.theme))
                    history = getattr(self, "_history", None)
                    if history is not None:
                        history.append_string(event.prompt)
                elif isinstance(event, TurnInputFailed):
                    await self._write(
                        system_message(
                            f"Queued input failed: {event.error}", error=True
                        )
                    )
                elif isinstance(event, AttachmentLoaded):
                    await self._flush_tool_activity()
                    await self._write(system_message(event.display))
                elif isinstance(event, TextStarted):
                    await self._flush_tool_activity()
                    self._stream_text = ""
                    self._stream_answer_started = False
                    self._reset_stream_projection()
                elif isinstance(event, TextDelta):
                    await self._flush_tool_activity()
                    await self._append_assistant_text(event.text)
                elif isinstance(event, TextCompleted):
                    await self._flush_tool_activity()
                    await self._commit_assistant_text(event.text)
                elif isinstance(event, PlanStarted):
                    await self._flush_tool_activity()
                    self._stream_plan = ""
                elif isinstance(event, PlanDelta):
                    self._stream_plan += event.text
                elif isinstance(event, PlanCompleted):
                    self._stream_plan = ""
                    if event.plan:
                        self._pending_plan = event.plan
                        await self._write(
                            assistant_message("## Proposed plan\n\n" + event.plan)
                        )
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
                elif isinstance(event, ModelStepCompleted):
                    await self._commit_model_step(event)
                elif isinstance(event, ModelRequestPrepared):
                    if (
                        self._display_density.includes(DisplayDensity.DETAILED)
                        and event.injections
                    ):
                        await self._flush_tool_activity()
                        await self._write(_detailed_context_group(event))
                        self._blocks.mark_work()
                elif isinstance(event, CompactionStarted):
                    self._update_agent_activity(
                        compaction_activity_label(event.trigger)
                    )
                elif isinstance(event, CompactionCompleted):
                    await self._commit_compaction(event, "my-code is working…")
                elif isinstance(event, ToolStarted):
                    if self._display_density.includes(DisplayDensity.DETAILED):
                        await self._flush_tool_activity()
                        await self._write(_detailed_tool_call(event))
                    if self._tool_activity is None:
                        self._tool_activity = ToolActivityGroup()
                    self._tool_activity.start(event.tool_use_id, event.presentation)
                    self._update_agent_activity(event.presentation.activity)
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
                    self._update_agent_activity("my-code is working…")
                elif isinstance(event, TodoListUpdated):
                    await self._flush_tool_activity()
                    self._todos = event.todos
                    await self._write(todo_snapshot(event.todos))
                elif isinstance(event, ContextUpdated):
                    self._apply_context_update(event.status)
                elif isinstance(event, TurnSucceeded):
                    await self._retire_transient_content()
                    await self._flush_tool_activity()
                    await self._flush_unclassified_blocks()
                    await self._write(
                        system_message(
                            f"Done · {event.completed_steps} steps · "
                            f"{event.input_tokens} input / "
                            f"{event.output_tokens} output tokens"
                        )
                    )
                    if (
                        self._pending_plan
                        and not getattr(self.application, "queued_inputs", lambda: ())()
                    ):
                        self._panel = "plan_action"
                        self._panel_picker.reset()
                elif isinstance(event, MaxStepsReached):
                    await self._retire_transient_content()
                    await self._flush_tool_activity()
                    await self._flush_unclassified_blocks()
                    await self._write(
                        system_message(
                            "Max steps reached "
                            f"({event.completed_steps}/{event.max_steps}) · "
                            f"{event.input_tokens} input / "
                            f"{event.output_tokens} output tokens",
                            error=True,
                        )
                    )
                self._invalidate_for_event(event)
        except asyncio.CancelledError:
            await self._retire_transient_content()
            await self._interrupt_and_flush_tools()
            await self._flush_unclassified_blocks()
            await self._write(system_message("Turn interrupted.", error=True))
            raise
        except Exception as error:
            await self._retire_transient_content()
            await self._interrupt_and_flush_tools()
            await self._flush_unclassified_blocks()
            await self._write(system_message(f"Error: {error}", error=True))
        finally:
            await self._retire_transient_content()
            await self._interrupt_and_flush_tools()
            await self._flush_unclassified_blocks()
            self._agent_active = False
            self._busy = False
            self._end_agent_activity()
            self._refresh_status()

    async def _consume_background_event(self, event: TurnEvent) -> None:
        if isinstance(event, TurnInputAccepted):
            self._blocks.reset_group()
            if self._display_density.includes(DisplayDensity.DETAILED):
                await self._write(block_separator("User input"))
            await self._write(user_message(event.prompt, self.theme))
            history = getattr(self, "_history", None)
            if history is not None:
                history.append_string(event.prompt)
        elif isinstance(event, TurnInputFailed):
            await self._write(
                system_message(f"Queued input failed: {event.error}", error=True)
            )
        elif isinstance(event, TextDelta):
            await self._flush_tool_activity()
            await self._append_assistant_text(event.text)
        elif isinstance(event, TextCompleted):
            await self._flush_tool_activity()
            await self._commit_assistant_text(event.text)
        elif isinstance(event, PlanCompleted) and event.plan:
            self._stream_plan = ""
            await self._write(assistant_message("## Proposed plan\n\n" + event.plan))
        elif isinstance(event, ReasoningDelta):
            await self._flush_tool_activity()
            while len(self._reasoning_parts) <= event.part_index:
                self._reasoning_parts.append("")
            self._reasoning_parts[event.part_index] += event.text
        elif isinstance(event, ReasoningCompleted):
            await self._flush_tool_activity()
            await self._commit_reasoning(event)
        elif isinstance(event, ModelStepCompleted):
            await self._commit_model_step(event)
        elif isinstance(event, ModelRequestPrepared):
            if (
                self._display_density.includes(DisplayDensity.DETAILED)
                and event.injections
            ):
                await self._flush_tool_activity()
                await self._write(_detailed_context_group(event))
                self._blocks.mark_work()
        elif isinstance(event, CompactionStarted):
            self._update_agent_activity(compaction_activity_label(event.trigger))
        elif isinstance(event, CompactionCompleted):
            await self._commit_compaction(event, "Handling background task…")
        elif isinstance(event, ToolStarted):
            if self._display_density.includes(DisplayDensity.DETAILED):
                await self._flush_tool_activity()
                await self._write(_detailed_tool_call(event))
            if self._tool_activity is None:
                self._tool_activity = ToolActivityGroup()
            self._tool_activity.start(event.tool_use_id, event.presentation)
            self._update_agent_activity(event.presentation.activity)
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
            self._update_agent_activity("Handling background task…")
        elif isinstance(event, TodoListUpdated):
            await self._flush_tool_activity()
            self._todos = event.todos
            await self._write(todo_snapshot(event.todos))
        elif isinstance(event, ContextUpdated):
            self._apply_context_update(event.status)
        elif isinstance(event, TurnSucceeded):
            await self._retire_transient_content()
            await self._flush_tool_activity()
            await self._flush_unclassified_blocks()
            await self._write(
                system_message(
                    f"Background done · {event.completed_steps} steps · "
                    f"{event.input_tokens} input / {event.output_tokens} output tokens"
                )
            )
        elif isinstance(event, MaxStepsReached):
            await self._retire_transient_content()
            await self._flush_tool_activity()
            await self._flush_unclassified_blocks()
            await self._write(
                system_message(
                    f"Background continuation reached max steps ({event.max_steps}).",
                    error=True,
                )
            )

    def _apply_context_update(self, status: ContextUsageView) -> None:
        self._context_status = status
        if self._status is not None:
            self._status = replace(
                self._status,
                context_entry_count=status.context_entry_count,
                conversation_entry_count=status.conversation_entry_count,
            )

    async def _commit_compaction(
        self, event: CompactionCompleted, resume_label: str
    ) -> None:
        self._apply_context_update(event.status)
        await self._flush_tool_activity()
        await self._write(
            system_message(compaction_completed_message(event.trigger, event.status))
        )
        self._blocks.mark_work()
        self._update_agent_activity(resume_label)

    async def _commit_assistant_text(self, text: str) -> None:
        """仅固化未提交尾部；step 仍负责 reasoning 与分隔符收尾。"""

        self._stream_text = text
        fragments = self._flush_stream_projection()
        await self._commit_stream_fragments(fragments)
        self._stream_text = ""
        if not self._stream_answer_started:
            self._blocks.add_text(text)

    async def _commit_reasoning(self, event: ReasoningCompleted) -> None:
        """Hold reasoning so its order with the step text remains stable."""

        self._reasoning_parts = []
        self._stream_plan = ""
        self._blocks.add_reasoning(event.presentation)

    async def _commit_model_step(self, event: ModelStepCompleted) -> None:
        await self._write_many(
            self._blocks.complete_step(
                has_tools=event.has_tools,
                label_answer=self._display_density.includes(DisplayDensity.DETAILED),
            )
        )

    async def _flush_unclassified_blocks(self) -> None:
        await self._write_many(self._blocks.drain_unclassified())

    async def _append_assistant_text(self, text: str) -> None:
        self._stream_text += text
        fragments = self._update_stream_projection()
        await self._commit_stream_fragments(fragments)

    async def _commit_stream_fragments(self, fragments: tuple[str, ...]) -> None:
        if not fragments:
            return
        first = not self._stream_answer_started
        if first:
            await self._write_many(
                self._blocks.begin_streamed_text(
                    label_answer=self._display_density.includes(DisplayDensity.DETAILED)
                )
            )
            self._stream_answer_started = True
        for fragment in fragments:
            await self._write_stream_fragment(fragment, first=first)
            first = False

    async def _retire_transient_content(self) -> None:
        """统一固化未完成回答的动态尾部，并清除 reasoning 投影。"""

        if self._stream_text:
            fragments = self._flush_stream_projection()
            await self._commit_stream_fragments(fragments)
        self._stream_text = ""
        self._reasoning_parts = []

    async def _flush_tool_activity(self) -> None:
        activity = self._tool_activity
        if activity is None:
            return
        self._tool_activity = None
        if activity:
            await self._write(tool_activity_message(activity))
            self._blocks.mark_work()

    async def _interrupt_and_flush_tools(self) -> None:
        if self._tool_activity is not None:
            self._tool_activity.interrupt_running()
        await self._flush_tool_activity()


def _is_todo(use: object) -> bool:
    display_name = getattr(use, "display_name", "")
    return display_name in {"TodoWrite", "Update Todos"}


def _detailed_context_group(event: ModelRequestPrepared) -> RenderableType:
    return injected_context_message(event.request_number, event.injections)


def _detailed_tool_call(event: ToolStarted) -> RenderableType:
    name = event.name or event.presentation.display_name
    return detailed_tool_call_message(name, event.input)


__all__ = ["TurnFlowMixin"]
