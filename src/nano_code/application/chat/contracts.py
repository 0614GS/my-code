"""Frontend-neutral contracts for interactive chat applications."""

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from nano_code.features.todos.models import TodoItem
from nano_code.model import (
    JsonObject,
    ReasoningDisclosure,
    ReasoningPresentation,
)
from nano_code.permissions import PermissionConfirmation, PermissionUpdate
from nano_code.providers.manager import ProviderUpdate, ProviderView
from nano_code.sessions import SessionSummary
from nano_code.tools import (
    ToolResultPresentation,
    ToolUsePresentation,
)


@dataclass(frozen=True, slots=True)
class PathSuggestion:
    path: str
    is_directory: bool
    display: str


@dataclass(frozen=True, slots=True)
class TurnSucceeded:
    text: str
    completed_steps: int
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True, slots=True)
class MaxStepsReached:
    max_steps: int
    completed_steps: int
    input_tokens: int
    output_tokens: int


type TurnOutcome = TurnSucceeded | MaxStepsReached


@dataclass(frozen=True, slots=True)
class RuntimeStatus:
    session_id: str
    cwd: str
    provider_id: str
    base_url: str | None
    model: str
    permission_mode: str
    credential_source: str
    working_message_count: int
    todos: tuple[TodoItem, ...]


@dataclass(frozen=True, slots=True)
class ContextStatus:
    estimated_input_tokens: int
    reserved_output_tokens: int
    estimated_total_tokens: int
    message_chars: int
    system_chars: int
    tool_schema_chars: int
    message_limit_chars: int
    working_message_count: int
    replacement_count: int
    compact_count: int
    user_context_chars: int = 0
    attachment_chars: int = 0
    input_tokens: int = 0
    input_limit_tokens: int = 200_000
    compact_trigger_tokens: int = 180_000
    remaining_input_tokens: int = 0
    measurement: str = "tokenizer_estimate"
    model_limit_source: str = "fallback"
    configured_compact_trigger_tokens: int | None = None
    warning: str | None = None


@dataclass(frozen=True, slots=True)
class PermissionRequest:
    tool_name: str
    tool_input: JsonObject
    message: str
    presentation: ToolUsePresentation
    suggestions: tuple[PermissionUpdate, ...] = ()


@dataclass(frozen=True, slots=True)
class TextStarted:
    pass


@dataclass(frozen=True, slots=True)
class TextDelta:
    text: str


@dataclass(frozen=True, slots=True)
class TextCompleted:
    text: str


@dataclass(frozen=True, slots=True)
class ReasoningStarted:
    disclosure: ReasoningDisclosure


@dataclass(frozen=True, slots=True)
class ReasoningDelta:
    disclosure: ReasoningDisclosure
    part_index: int
    text: str


@dataclass(frozen=True, slots=True)
class ReasoningCompleted:
    presentation: ReasoningPresentation


@dataclass(frozen=True, slots=True)
class AttachmentLoaded:
    path: str
    is_directory: bool
    display: str


@dataclass(frozen=True, slots=True)
class ToolStarted:
    tool_use_id: str
    presentation: ToolUsePresentation


@dataclass(frozen=True, slots=True)
class ToolFinished:
    tool_use_id: str
    is_error: bool
    presentation: ToolResultPresentation


@dataclass(frozen=True, slots=True)
class TodoListUpdated:
    todos: tuple[TodoItem, ...]


@dataclass(frozen=True, slots=True)
class TurnCompleted:
    result: TurnSucceeded


@dataclass(frozen=True, slots=True)
class StepLimitReached:
    result: MaxStepsReached


type TurnEvent = (
    AttachmentLoaded
    | TextStarted
    | TextDelta
    | TextCompleted
    | ReasoningStarted
    | ReasoningDelta
    | ReasoningCompleted
    | ToolStarted
    | ToolFinished
    | TodoListUpdated
    | TurnCompleted
    | StepLimitReached
)


@dataclass(frozen=True, slots=True)
class HistoryUserMessage:
    text: str


@dataclass(frozen=True, slots=True)
class HistoryAssistantMessage:
    text: str


@dataclass(frozen=True, slots=True)
class HistoryReasoning:
    presentation: ReasoningPresentation


@dataclass(frozen=True, slots=True)
class HistorySystemMessage:
    text: str


@dataclass(frozen=True, slots=True)
class HistoryToolCall:
    tool_use_id: str
    use: ToolUsePresentation
    result: ToolResultPresentation
    is_error: bool


type HistoryEntry = (
    HistoryUserMessage
    | HistoryAssistantMessage
    | HistoryReasoning
    | HistorySystemMessage
    | HistoryToolCall
)


@dataclass(frozen=True, slots=True)
class ResumedSession:
    status: RuntimeStatus
    history: tuple[HistoryEntry, ...]


type PermissionHandler = Callable[
    [PermissionRequest], Awaitable[PermissionConfirmation]
]


class ChatRuntime(Protocol):
    """Capabilities required by an interactive chat frontend."""

    async def submit(self, prompt: str) -> TurnOutcome: ...

    def stream(self, prompt: str) -> AsyncIterator[TurnEvent]: ...

    async def suggest_paths(self, query: str) -> tuple[PathSuggestion, ...]: ...

    def status(self) -> RuntimeStatus: ...

    def context_status(self) -> ContextStatus: ...

    async def compact(self) -> ContextStatus: ...

    def set_permission_handler(self, handler: PermissionHandler) -> None: ...

    def providers(self) -> tuple[ProviderView, ...]: ...

    async def refresh_provider_models(self, provider_id: str) -> ProviderView: ...

    async def configure_provider(self, update: ProviderUpdate) -> RuntimeStatus: ...

    async def list_sessions(self) -> tuple[SessionSummary, ...]: ...

    async def resume_session(self, session_id: str) -> ResumedSession: ...
