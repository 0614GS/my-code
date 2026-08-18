"""将完整响应 provider 适配为统一模型调用流。"""

from collections.abc import AsyncIterator

from nano_code.agent.contracts.model import ModelRequest, ModelStreamEvent
from nano_code.agent.ports.model import ModelCallPort, ModelCompletionPort
from nano_code.providers.streaming import (
    ModelStreamSequencer,
    completed_output_payloads,
)


class CompleteModelCallAdapter(ModelCallPort):
    """为非流式/简单 provider 提供单事件的流式适配层。"""

    def __init__(self, provider: ModelCompletionPort) -> None:
        self.provider = provider

    def stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
        return self._stream(request)

    async def _stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
        response = await self.provider.complete(request)
        sequencer = ModelStreamSequencer()
        for payload in completed_output_payloads(response):
            yield sequencer.emit(payload)
