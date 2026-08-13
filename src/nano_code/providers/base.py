"""智能体循环消费的 provider 无关边界。"""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from nano_code.messages import ChatMessage, ModelResponse
from nano_code.providers.events import ModelStreamEvent
from nano_code.tools.base import ToolDefinition


@dataclass(frozen=True, slots=True)
class ModelRequest:
    """上下文投影后的一次完整模型请求。"""

    system_prompt: str
    messages: tuple[ChatMessage, ...]
    tools: tuple[ToolDefinition, ...]
    max_output_tokens: int


class ModelProvider(Protocol):
    async def complete(self, request: ModelRequest) -> ModelResponse:
        """返回一条完整 assistant 响应。"""
        ...


@runtime_checkable
class StreamingModelProvider(Protocol):
    def stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
        """依次产出展示增量，并最终产出且仅产出一条完整响应。"""
        ...
