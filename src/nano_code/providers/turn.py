"""将完整响应 provider 适配为统一模型回合流。"""

from collections.abc import AsyncIterator

from nano_code.agent.contracts.context import ContextPlan
from nano_code.agent.contracts.model import (
    ModelResponseCompleted,
    ModelStreamEvent,
)
from nano_code.agent.ports.model import ModelCompletionPort, ModelTurnPort


class CompleteModelTurnAdapter(ModelTurnPort):
    """为 legacy/简单 provider 提供单事件的流式兼容层。"""

    def __init__(self, provider: ModelCompletionPort) -> None:
        self.provider = provider

    def stream(self, request: ContextPlan) -> AsyncIterator[ModelStreamEvent]:
        return self._stream(request)

    async def _stream(self, request: ContextPlan) -> AsyncIterator[ModelStreamEvent]:
        response = await self.provider.complete(request)
        yield ModelResponseCompleted(response)


ModelTurnAdapter = CompleteModelTurnAdapter
