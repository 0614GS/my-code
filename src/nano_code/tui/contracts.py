"""终端前端与对话运行时之间的窄接口。"""

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from nano_code.messages import JsonObject
from nano_code.permissions import PermissionConfirmation, PermissionUpdate
from nano_code.presentation import ToolResultPresentation, ToolUsePresentation
from nano_code.providers.manager import ProviderUpdate, ProviderView
from nano_code.sessions import SessionSummary
from nano_code.todos.models import TodoItem


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


@dataclass(frozen=True, slots=True)
class PermissionRequest:
    tool_name: str
    tool_input: JsonObject
    message: str
    presentation: ToolUsePresentation
    suggestions: tuple[PermissionUpdate, ...] = ()


@dataclass(frozen=True, slots=True)
class TextDelta:
    text: str


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
    | TextDelta
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
    """TUI 所需能力；核心实现类型不会泄漏到此处。"""

    async def submit(self, prompt: str) -> TurnOutcome:
        """运行一个用户 Turn。"""
        ...

    def stream(self, prompt: str) -> AsyncIterator[TurnEvent]:
        """运行一个用户 Turn，并产出可安全展示的生命周期事件。"""
        ...

    async def suggest_paths(self, query: str) -> tuple[PathSuggestion, ...]:
        """Return bounded workspace path suggestions."""
        ...

    def status(self) -> RuntimeStatus:
        """返回安全且不含凭据的运行时快照。"""
        ...

    def context_status(self) -> ContextStatus:
        """返回不修改上下文的安全预算快照。"""
        ...

    async def compact(self) -> ContextStatus:
        """执行一次手动 compact，并返回新的预算快照。"""
        ...

    def set_permission_handler(self, handler: PermissionHandler) -> None:
        """将权限提示路由到当前前端。"""

    def providers(self) -> tuple[ProviderView, ...]:
        """返回不含凭据的 provider profile。"""
        ...

    async def configure_provider(self, update: ProviderUpdate) -> RuntimeStatus:
        """持久化、激活并热切换一个 provider profile。"""
        ...

    async def list_sessions(self) -> tuple[SessionSummary, ...]:
        """列出当前项目中除活动会话外的可恢复会话。"""
        ...

    async def resume_session(self, session_id: str) -> ResumedSession:
        """严格加载并切换会话，返回与核心消息类型解耦的历史投影。"""
        ...
