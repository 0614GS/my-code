"""Bounded, frontend-neutral activity projection for child agent runs."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from my_code.agent.events import (
    AgentEvent,
    AgentReasoningCompleted,
    AgentReasoningDelta,
    AgentTextCompleted,
    AgentTextDelta,
    AgentToolFinished,
    AgentToolStarted,
)
from my_code.agent.models import AgentMaxStepsReached, AgentTurnSucceeded
from my_code.features.subagents.models import SubagentType
from my_code.model.primitives import TokenUsage
from my_code.tasks.models import SubagentActivityView, SubagentTaskView, TaskSnapshot

_MAX_EVENTS = 200
_MAX_STREAM_CHARS = 20_000


@dataclass(slots=True)
class SubagentActivityRecord:
    task_id: str
    run_id: str
    owner_run_id: str
    agent_type: SubagentType
    description: str
    background: bool
    events: deque[SubagentActivityView] = field(
        default_factory=lambda: deque(maxlen=_MAX_EVENTS)
    )
    text_stream: str = ""
    reasoning_stream: str = ""
    usage: TokenUsage = TokenUsage()

    def consume(self, event: AgentEvent) -> None:
        if isinstance(event, AgentTextDelta):
            self.text_stream = _tail(self.text_stream + event.text)
        elif isinstance(event, AgentTextCompleted):
            self.events.append(SubagentActivityView("text", event.text))
            self.text_stream = ""
        elif isinstance(event, AgentReasoningDelta):
            if event.disclosure in {"verbatim", "summary"}:
                self.reasoning_stream = _tail(self.reasoning_stream + event.text)
        elif isinstance(event, AgentReasoningCompleted):
            if event.presentation.disclosure in {"verbatim", "summary"}:
                self.events.append(
                    SubagentActivityView(
                        "reasoning", " ".join(event.presentation.parts)
                    )
                )
            else:
                self.events.append(
                    SubagentActivityView("reasoning", "Reasoning hidden")
                )
            self.reasoning_stream = ""
        elif isinstance(event, AgentToolStarted):
            self.events.append(
                SubagentActivityView(
                    "tool_started",
                    event.presentation.activity,
                    event.presentation.summary,
                )
            )
        elif isinstance(event, AgentToolFinished):
            self.events.append(
                SubagentActivityView(
                    "tool_finished",
                    event.presentation.summary,
                    event.presentation.detail,
                    event.is_error,
                )
            )
        elif isinstance(event, (AgentTurnSucceeded, AgentMaxStepsReached)):
            self.usage = event.usage

    def view(self, task: TaskSnapshot) -> SubagentTaskView:
        failure = task.failure.message if task.failure is not None else None
        return SubagentTaskView(
            task_id=self.task_id,
            run_id=self.run_id,
            agent_type=self.agent_type.value,
            description=self.description,
            background=self.background,
            status=task.status.value,
            created_at=task.created_at,
            started_at=task.started_at,
            finished_at=task.finished_at,
            input_tokens=self.usage.total_input_tokens,
            output_tokens=self.usage.output_tokens,
            reasoning=self.reasoning_stream,
            text=self.text_stream,
            activities=tuple(self.events),
            error=failure,
        )


def _tail(value: str) -> str:
    return value[-_MAX_STREAM_CHARS:]


__all__ = ["SubagentActivityRecord"]
