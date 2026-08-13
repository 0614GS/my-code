"""Provider-neutral boundary consumed by the agent loop."""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from nano_code.messages import ChatMessage, ModelResponse
from nano_code.providers.events import ModelStreamEvent
from nano_code.tools.base import ToolDefinition


@dataclass(frozen=True, slots=True)
class ModelRequest:
    """One complete model request after context projection."""

    system_prompt: str
    messages: tuple[ChatMessage, ...]
    tools: tuple[ToolDefinition, ...]
    max_output_tokens: int


class ModelProvider(Protocol):
    async def complete(self, request: ModelRequest) -> ModelResponse:
        """Return one complete assistant response."""
        ...


@runtime_checkable
class StreamingModelProvider(Protocol):
    def stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
        """Yield display deltas followed by exactly one complete response."""
        ...
