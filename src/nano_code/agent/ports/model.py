"""模型回合使用的 outbound ports。"""

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from nano_code.agent.contracts.model import ModelOutput, ModelRequest, ModelStreamEvent


@runtime_checkable
class ModelTurnPort(Protocol):
    """主 Agent Loop 使用的流式模型回合能力。"""

    def stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]: ...


@runtime_checkable
class ModelCompletionPort(Protocol):
    """compact 等独立请求使用的完整模型响应能力。"""

    async def complete(self, request: ModelRequest) -> ModelOutput: ...


__all__ = ["ModelCompletionPort", "ModelTurnPort"]
