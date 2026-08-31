"""Frontend-neutral activity views owned by the Subagent feature."""

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class SubagentTranscriptText:
    role: str
    text: str
    streaming: bool = False


@dataclass(frozen=True, slots=True)
class SubagentTranscriptReasoning:
    disclosure: Literal["verbatim", "summary", "redacted", "hidden"]
    parts: tuple[str, ...]
    streaming: bool = False


@dataclass(frozen=True, slots=True)
class SubagentToolUseView:
    display_name: str
    summary: str
    activity: str


@dataclass(frozen=True, slots=True)
class SubagentToolResultView:
    summary: str
    detail: str | None = None
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class SubagentTranscriptTool:
    tool_use_id: str
    use: SubagentToolUseView
    result: SubagentToolResultView | None = None
    is_error: bool = False


@dataclass(frozen=True, slots=True)
class SubagentActivityView:
    task_id: str
    run_id: str
    agent_type: str
    description: str
    background: bool
    status: str
    created_at: str
    started_at: str | None
    finished_at: str | None
    input_tokens: int
    output_tokens: int
    transcript: tuple[
        SubagentTranscriptText | SubagentTranscriptReasoning | SubagentTranscriptTool,
        ...,
    ]
    active_tool_ids: tuple[str, ...]
    error: str | None = None


__all__ = [
    "SubagentActivityView",
    "SubagentToolResultView",
    "SubagentToolUseView",
    "SubagentTranscriptReasoning",
    "SubagentTranscriptText",
    "SubagentTranscriptTool",
]
