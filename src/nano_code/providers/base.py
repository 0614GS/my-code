"""智能体循环消费的 provider 无关边界。"""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from nano_code.context.models import ContextPlan
from nano_code.messages import ModelResponse
from nano_code.providers.events import ModelStreamEvent

# 对外保留 ModelRequest 名称；其领域含义现在由 context 包中的计划对象定义。
ModelRequest = ContextPlan


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    """由 provider 声明、核心只读的可选协议能力。"""

    system_prompt_blocks: bool = False
    prompt_caching: bool = False
    max_prompt_cache_breakpoints: int = 0

    def __post_init__(self) -> None:
        if self.max_prompt_cache_breakpoints < 0:
            raise ValueError("Cache breakpoint count must not be negative")
        if self.prompt_caching and not self.system_prompt_blocks:
            raise ValueError("Prompt caching requires structured system blocks")
        if self.prompt_caching and self.max_prompt_cache_breakpoints < 1:
            raise ValueError("Prompt caching requires at least one breakpoint")


class ModelProvider(Protocol):
    @property
    def capabilities(self) -> ProviderCapabilities:
        """返回该适配器当前连接实际支持的能力。"""
        ...

    async def complete(self, request: ModelRequest) -> ModelResponse:
        """返回一条完整 assistant 响应。"""
        ...


@runtime_checkable
class StreamingModelProvider(Protocol):
    def stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
        """依次产出展示增量，并最终产出且仅产出一条完整响应。"""
        ...
