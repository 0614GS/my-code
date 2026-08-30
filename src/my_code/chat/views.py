"""Safe, frontend-neutral snapshots exposed by :mod:`my_code.chat`."""

from dataclasses import dataclass, field
from typing import Literal

from my_code.chat.history import HistoryEntry
from my_code.chat.status import ContextStatus, RuntimeStatus
from my_code.model.primitives import ReasoningPresentation
from my_code.sessions.request_audit import ResolvedAuditRequest


@dataclass(frozen=True, slots=True)
class TranscriptField:
    key: str
    value: "TranscriptValue"


@dataclass(frozen=True, slots=True)
class TranscriptValue:
    kind: Literal["scalar", "object", "array"]
    scalar: str | None = None
    fields: tuple[TranscriptField, ...] = ()
    items: tuple["TranscriptValue", ...] = ()


@dataclass(frozen=True, slots=True)
class TranscriptText:
    role: Literal["user", "assistant"]
    text: str
    is_final_answer: bool = False
    kind: Literal["text"] = field(default="text", init=False)


@dataclass(frozen=True, slots=True)
class TranscriptReasoning:
    presentation: ReasoningPresentation
    kind: Literal["reasoning"] = field(default="reasoning", init=False)


@dataclass(frozen=True, slots=True)
class TranscriptToolCall:
    name: str
    input: TranscriptValue
    kind: Literal["tool_call"] = field(default="tool_call", init=False)


@dataclass(frozen=True, slots=True)
class TranscriptToolResult:
    name: str
    content: str
    is_error: bool
    kind: Literal["tool_result"] = field(default="tool_result", init=False)


@dataclass(frozen=True, slots=True)
class TranscriptSummary:
    content: str
    kind: Literal["summary"] = field(default="summary", init=False)


@dataclass(frozen=True, slots=True)
class TranscriptAttachment:
    attachment_kind: str
    value: TranscriptValue
    kind: Literal["attachment"] = field(default="attachment", init=False)


type TranscriptEntry = (
    TranscriptText
    | TranscriptReasoning
    | TranscriptToolCall
    | TranscriptToolResult
    | TranscriptSummary
    | TranscriptAttachment
)


@dataclass(frozen=True, slots=True)
class TranscriptView:
    revision: int
    entries: tuple[TranscriptEntry, ...]
    requests: tuple[ResolvedAuditRequest, ...] = ()
    audit_legacy_missing: bool = False


@dataclass(frozen=True, slots=True)
class SessionView:
    status: RuntimeStatus
    history: tuple[HistoryEntry, ...]


@dataclass(frozen=True, slots=True)
class SessionUsageView:
    request_count: int
    input_tokens: int
    cache_creation_input_tokens: int
    cache_read_input_tokens: int
    output_tokens: int
    context: ContextStatus

    @property
    def total_input_tokens(self) -> int:
        return (
            self.input_tokens
            + self.cache_creation_input_tokens
            + self.cache_read_input_tokens
        )


@dataclass(frozen=True, slots=True)
class ToolCapabilityView:
    name: str
    description: str
    source: str
    exposure: str = "eager"
    access: str = "direct"


@dataclass(frozen=True, slots=True)
class SkillCapabilityView:
    name: str
    description: str
    source: str
    compatibility: str | None = None


@dataclass(frozen=True, slots=True)
class CapabilityDiagnosticView:
    source: str
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class McpServerView:
    name: str
    state: str
    tool_names: tuple[str, ...]
    diagnostic: CapabilityDiagnosticView | None = None


@dataclass(frozen=True, slots=True)
class CapabilitiesView:
    tools: tuple[ToolCapabilityView, ...]
    skills: tuple[SkillCapabilityView, ...]
    skill_diagnostics: tuple[CapabilityDiagnosticView, ...]
    mcp_servers: tuple[McpServerView, ...]
    tool_search_mode: str = "dispatcher"

    @property
    def enabled_summary(self) -> str:
        connected = sum(server.state == "connected" for server in self.mcp_servers)
        return (
            f"{len(self.tools)} tools · {len(self.skills)} skills · "
            f"{connected}/{len(self.mcp_servers)} MCP"
        )


@dataclass(frozen=True, slots=True)
class BackgroundTaskView:
    task_id: str
    task_type: str
    summary: str
    status: str
    created_at: str
    started_at: str | None
    finished_at: str | None
    output_path: str | None
    error: str | None


@dataclass(frozen=True, slots=True)
class SubagentTaskView:
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
    transcript: tuple[HistoryEntry, ...]
    active_tool_ids: tuple[str, ...]
    error: str | None = None


__all__ = [
    "BackgroundTaskView",
    "CapabilitiesView",
    "CapabilityDiagnosticView",
    "McpServerView",
    "SessionUsageView",
    "SessionView",
    "SkillCapabilityView",
    "SubagentTaskView",
    "ToolCapabilityView",
    "TranscriptAttachment",
    "TranscriptEntry",
    "TranscriptField",
    "TranscriptReasoning",
    "TranscriptSummary",
    "TranscriptText",
    "TranscriptToolCall",
    "TranscriptToolResult",
    "TranscriptValue",
    "TranscriptView",
]
