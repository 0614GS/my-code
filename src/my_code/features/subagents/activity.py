"""Bounded, frontend-neutral activity projection for child agent runs."""

from __future__ import annotations

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
from my_code.features.subagents.views import (
    SubagentActivityView,
    SubagentToolResultView,
    SubagentToolUseView,
    SubagentTranscriptReasoning,
    SubagentTranscriptText,
    SubagentTranscriptTool,
)
from my_code.model.primitives import (
    ReasoningDisclosure,
    TokenUsage,
)
from my_code.tasks.models import TaskSnapshot


@dataclass(slots=True)
class SubagentActivityRecord:
    task_id: str
    run_id: str
    owner_run_id: str
    agent_type: SubagentType
    description: str
    background: bool
    prompt: str
    transcript: list[
        SubagentTranscriptText | SubagentTranscriptReasoning | SubagentTranscriptTool
    ] = field(default_factory=list)
    text_stream: str = ""
    reasoning_stream: str = ""
    reasoning_disclosure: ReasoningDisclosure | None = None
    active_tools: dict[str, SubagentTranscriptTool] = field(default_factory=dict)
    usage: TokenUsage = TokenUsage()

    def __post_init__(self) -> None:
        self.transcript.append(SubagentTranscriptText("user", self.prompt))

    def consume(self, event: AgentEvent) -> None:
        if isinstance(event, AgentTextDelta):
            self.text_stream += event.text
        elif isinstance(event, AgentTextCompleted):
            self.transcript.append(SubagentTranscriptText("assistant", event.text))
            self.text_stream = ""
        elif isinstance(event, AgentReasoningDelta):
            if event.disclosure in {"verbatim", "summary"}:
                self.reasoning_disclosure = event.disclosure
                self.reasoning_stream += event.text
        elif isinstance(event, AgentReasoningCompleted):
            self.transcript.append(
                SubagentTranscriptReasoning(
                    event.presentation.disclosure, event.presentation.parts
                )
            )
            self.reasoning_stream = ""
            self.reasoning_disclosure = None
        elif isinstance(event, AgentToolStarted):
            item = SubagentTranscriptTool(
                event.tool_use_id,
                _tool_use(event.presentation),
            )
            self.transcript.append(item)
            self.active_tools[event.tool_use_id] = item
        elif isinstance(event, AgentToolFinished):
            started = self.active_tools.pop(event.tool_use_id, None)
            use = started.use if started is not None else _unknown_tool_use(event.name)
            completed = SubagentTranscriptTool(
                event.tool_use_id,
                use,
                SubagentToolResultView(
                    event.presentation.summary,
                    event.presentation.detail,
                    event.presentation.truncated,
                ),
                event.is_error,
            )
            if started is None:
                self.transcript.append(completed)
            else:
                index = self.transcript.index(started)
                self.transcript[index] = completed
        elif isinstance(event, (AgentTurnSucceeded, AgentMaxStepsReached)):
            self.usage = event.usage

    def view(self, task: TaskSnapshot) -> SubagentActivityView:
        failure = task.failure.message if task.failure is not None else None
        transcript = list(self.transcript)
        if self.reasoning_stream and self.reasoning_disclosure in {
            "verbatim",
            "summary",
        }:
            transcript.append(
                SubagentTranscriptReasoning(
                    self.reasoning_disclosure,
                    (self.reasoning_stream,),
                    streaming=True,
                )
            )
        if self.text_stream:
            transcript.append(
                SubagentTranscriptText("assistant", self.text_stream, streaming=True)
            )
        return SubagentActivityView(
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
            transcript=tuple(transcript),
            active_tool_ids=tuple(self.active_tools),
            error=failure,
        )


def _unknown_tool_use(name: str) -> SubagentToolUseView:
    return SubagentToolUseView(name, "Tool input unavailable", f"Running {name}")


def _tool_use(presentation: object) -> SubagentToolUseView:
    from my_code.tools.presentation import ToolUsePresentation

    assert isinstance(presentation, ToolUsePresentation)
    return SubagentToolUseView(
        presentation.display_name, presentation.summary, presentation.activity
    )


__all__ = ["SubagentActivityRecord"]
