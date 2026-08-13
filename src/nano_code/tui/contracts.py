"""Narrow contracts between a terminal frontend and a chat runtime."""

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from nano_code.messages import JsonObject
from nano_code.permissions import PermissionConfirmation
from nano_code.providers.manager import ProviderUpdate, ProviderView


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
type PermissionHandler = Callable[
    [PermissionRequest], Awaitable[PermissionConfirmation]
]


class ChatRuntime(Protocol):
    """Capabilities needed by the TUI; no core implementation types leak here."""

    async def submit(self, prompt: str) -> TurnResult:
        """Run one user turn."""
        ...

    def stream(self, prompt: str) -> AsyncIterator[TurnEvent]:
        """Run one user turn while yielding display-safe lifecycle events."""
        ...

    def status(self) -> RuntimeStatus:
        """Return a safe, credential-free runtime snapshot."""
        ...

    def set_permission_handler(self, handler: PermissionHandler) -> None:
        """Route permission prompts to the active frontend."""

    def providers(self) -> tuple[ProviderView, ...]:
        """Return credential-free provider profiles."""
        ...

    async def configure_provider(self, update: ProviderUpdate) -> RuntimeStatus:
        """Persist, activate, and hot-swap one provider profile."""
        ...
