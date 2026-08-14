"""智能体循环消费的 provider 无关边界。"""

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from nano_code.context.models import ContextPlan
from nano_code.messages import ModelResponse
from nano_code.providers.events import ModelStreamEvent

# 对外保留 ModelRequest 名称；其领域含义现在由 context 包中的计划对象定义。
ModelRequest = ContextPlan


class ModelProvider(Protocol):
    async def complete(self, request: ModelRequest) -> ModelResponse:
        """返回一条完整 assistant 响应。"""
        ...


@runtime_checkable
class StreamingModelProvider(Protocol):
    def stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
        """依次产出展示增量，并最终产出且仅产出一条完整响应。"""
        ...
