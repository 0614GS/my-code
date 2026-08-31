"""Owner-scoped background task and subagent projections."""

from collections.abc import Callable

from my_code.application.contracts.history import (
    HistoryEntry,
    HistoryReasoning,
    HistoryText,
    HistoryToolCall,
)
from my_code.application.contracts.views import (
    BackgroundTaskView,
    SubagentTaskView,
    TranscriptView,
)
from my_code.conversation.presentation import ToolResultPresentation
from my_code.features.background_tasks.registry import BackgroundTaskRegistry
from my_code.features.subagents.controller import SubagentController
from my_code.features.subagents.views import (
    SubagentTranscriptReasoning,
    SubagentTranscriptText,
    SubagentTranscriptTool,
)
from my_code.model.primitives import ReasoningPresentation
from my_code.sessions.session import Session
from my_code.tools.presentation import ToolUsePresentation, tool_display_category


class ActivityProjection:
    def __init__(
        self,
        background_tasks: BackgroundTaskRegistry | None,
        subagents: SubagentController | None,
        transcript_projector: Callable[[Session], TranscriptView],
    ) -> None:
        self._background_tasks = background_tasks
        self._subagents = subagents
        self._transcript_projector = transcript_projector

    @property
    def subagents(self) -> SubagentController | None:
        return self._subagents

    @property
    def background_registry(self) -> BackgroundTaskRegistry | None:
        return self._background_tasks

    def background_tasks(self, owner: str) -> tuple[BackgroundTaskView, ...]:
        registry = self._background_tasks
        if registry is None and self._subagents is None:
            return ()
        views: list[BackgroundTaskView] = []
        for item in () if registry is None else registry.tasks_for(owner):
            if item.task_type == "subagent" and self._subagents is not None:
                continue
            assert registry is not None
            snapshot = registry.tasks.snapshot(item.task_id)
            output_path = item.details.get("output_file")
            views.append(
                BackgroundTaskView(
                    task_id=item.task_id,
                    task_type=item.task_type,
                    summary=item.summary,
                    status=snapshot.status.value,
                    created_at=snapshot.created_at,
                    started_at=snapshot.started_at,
                    finished_at=snapshot.finished_at,
                    output_path=(output_path if isinstance(output_path, str) else None),
                    error=(
                        snapshot.failure.message
                        if snapshot.failure is not None
                        else None
                    ),
                )
            )
        views.extend(
            BackgroundTaskView(
                task_id=item.task_id,
                task_type=(
                    "subagent/background" if item.background else "subagent/foreground"
                ),
                summary=item.description,
                status=item.status,
                created_at=item.created_at,
                started_at=item.started_at,
                finished_at=item.finished_at,
                output_path=None,
                error=item.error,
            )
            for item in self.subagent_tasks(owner)
        )
        return tuple(views)

    def subagent_tasks(self, owner: str) -> tuple[SubagentTaskView, ...]:
        if self._subagents is None:
            return ()
        return tuple(
            SubagentTaskView(
                task_id=item.task_id,
                run_id=item.run_id,
                agent_type=item.agent_type,
                description=item.description,
                background=item.background,
                status=item.status,
                created_at=item.created_at,
                started_at=item.started_at,
                finished_at=item.finished_at,
                input_tokens=item.input_tokens,
                output_tokens=item.output_tokens,
                transcript=tuple(_project_entry(entry) for entry in item.transcript),
                active_tool_ids=item.active_tool_ids,
                error=item.error,
            )
            for item in self._subagents.task_views(owner)
        )

    def subagent_transcript(self, task_id: str) -> TranscriptView:
        session = (
            self._subagents.session_for_task(task_id)
            if self._subagents is not None
            else None
        )
        if session is None:
            raise ValueError(f"Subagent transcript is unavailable: {task_id}")
        return self._transcript_projector(session)


def _project_entry(
    entry: SubagentTranscriptText
    | SubagentTranscriptReasoning
    | SubagentTranscriptTool,
) -> HistoryEntry:
    if isinstance(entry, SubagentTranscriptText):
        role = "user" if entry.role == "user" else "assistant"
        return HistoryText(role, entry.text, entry.streaming)
    if isinstance(entry, SubagentTranscriptReasoning):
        return HistoryReasoning(
            ReasoningPresentation(entry.disclosure, entry.parts), entry.streaming
        )
    use = ToolUsePresentation(
        entry.use.display_name,
        entry.use.summary,
        entry.use.activity,
        tool_display_category(entry.use.display_name),
    )
    result = (
        ToolResultPresentation(
            entry.result.summary, entry.result.detail, entry.result.truncated
        )
        if entry.result is not None
        else ToolResultPresentation("Tool is still running.")
    )
    return HistoryToolCall(
        entry.tool_use_id,
        use,
        result,
        entry.is_error,
        running=entry.result is None,
        name=entry.use.display_name,
    )


__all__ = ["ActivityProjection"]
