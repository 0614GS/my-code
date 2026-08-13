"""终端前端与对话运行时之间的窄接口。"""

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from nano_code.messages import JsonObject
from nano_code.permissions import PermissionConfirmation
from nano_code.providers.manager import ProviderUpdate, ProviderView
from nano_code.sessions import SessionSummary


@dataclass(frozen=True, slots=True)
class TurnResult:
    text: str
    turns: int
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True, slots=True)
class RuntimeStatus:
    session_id: str
    cwd: str
    provider_id: str
    base_url: str | None
    model: str
    permission_mode: str
    credential_source: str
    message_count: int


@dataclass(frozen=True, slots=True)
class PermissionRequest:
    tool_name: str
    tool_input: JsonObject
    message: str


@dataclass(frozen=True, slots=True)
class TextDelta:
    text: str


@dataclass(frozen=True, slots=True)
class ToolStarted:
    tool_use_id: str
    name: str
    input: JsonObject


@dataclass(frozen=True, slots=True)
class ToolFinished:
    tool_use_id: str
    name: str
    content: str
    is_error: bool


@dataclass(frozen=True, slots=True)
class TurnCompleted:
    result: TurnResult


type TurnEvent = TextDelta | ToolStarted | ToolFinished | TurnCompleted


@dataclass(frozen=True, slots=True)
class HistoryUserMessage:
    text: str


@dataclass(frozen=True, slots=True)
class HistoryAssistantMessage:
    text: str


@dataclass(frozen=True, slots=True)
class HistoryToolCall:
    tool_use_id: str
    name: str
    input: JsonObject
    result: str
    is_error: bool


type HistoryEntry = HistoryUserMessage | HistoryAssistantMessage | HistoryToolCall


@dataclass(frozen=True, slots=True)
class ResumedSession:
    status: RuntimeStatus
    history: tuple[HistoryEntry, ...]


type PermissionHandler = Callable[
    [PermissionRequest], Awaitable[PermissionConfirmation]
]


class ChatRuntime(Protocol):
    """TUI 所需能力；核心实现类型不会泄漏到此处。"""

    async def submit(self, prompt: str) -> TurnResult:
        """运行一个用户轮次。"""
        ...

    def stream(self, prompt: str) -> AsyncIterator[TurnEvent]:
        """运行一个用户轮次，并产出可安全展示的生命周期事件。"""
        ...

    def status(self) -> RuntimeStatus:
        """返回安全且不含凭据的运行时快照。"""
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
