"""将完整响应 provider 适配为统一模型调用流。"""

from collections.abc import AsyncIterator

from nano_code.agent.contracts.model import (
    ModelOutputCompleted,
    ModelRequest,
    ModelStreamEvent,
)
from nano_code.agent.ports.model import ModelCallPort, ModelCompletionPort


class CompleteModelCallAdapter(ModelCallPort):
    """为非流式/简单 provider 提供单事件的流式适配层。"""

    def __init__(self, provider: ModelCompletionPort) -> None:
        self.provider = provider

    def stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
        return self._stream(request)

    async def _stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
        response = await self.provider.complete(request)
        yield ModelOutputCompleted(response)
