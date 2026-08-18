"""用于在 ModelCall 之间原子切换 provider 连接的运行时路由器。"""

import asyncio
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from nano_code.agent.contracts.model import ModelOutput, ModelRequest, ModelStreamEvent
from nano_code.agent.ports.model import ModelCallPort, ModelCompletionPort
from nano_code.auth import CredentialSource
from nano_code.conversation import ProviderBinding
from nano_code.providers.anthropic import AnthropicProvider
from nano_code.providers.base import ProviderCapabilities
from nano_code.providers.call import CompleteModelCallAdapter
from nano_code.providers.openai_responses import OpenAIResponsesProvider
from nano_code.providers.profiles import ProviderProtocol, ReasoningConfig


@dataclass(frozen=True, slots=True)
class ProviderConnection:
    """已完整解析、可用于构造 SDK 适配器的 profile。"""

    id: str
    protocol: ProviderProtocol
    model: str
    base_url: str | None
    api_key: str | None
    credential_source: CredentialSource
    reasoning: ReasoningConfig = ReasoningConfig()


type ProviderFactory = Callable[[ProviderConnection], ModelCompletionPort]


@runtime_checkable
class _ClosableProvider(Protocol):
    async def close(self) -> None:
        """释放 provider 持有的网络资源。"""


@runtime_checkable
class _StreamingProvider(Protocol):
    def stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
        """可选的原生 streaming adapter 能力。"""
        ...


class ProviderRouter(ModelCallPort, ModelCompletionPort):
    """串行化请求与切换，同时保持智能体循环协议。"""

    def __init__(
        self,
        connection: ProviderConnection,
        *,
        factory: ProviderFactory | None = None,
    ) -> None:
        self._factory = factory or _build_provider
        self._connection = connection
        self._provider: ModelCompletionPort | None = None
        self._lock = asyncio.Lock()

    @property
    def connection(self) -> ProviderConnection:
        return self._connection

    @property
    def binding(self) -> ProviderBinding:
        connection = self._connection
        return ProviderBinding(
            connection.protocol.value,
            connection.id,
            connection.model,
            connection.base_url,
        )

    @property
    def capabilities(self) -> ProviderCapabilities:
        """无需提前创建网络客户端即可暴露当前连接能力。"""

        match self._connection.protocol:
            case ProviderProtocol.ANTHROPIC_MESSAGES:
                return AnthropicProvider.capabilities_for(self._connection.base_url)
            case ProviderProtocol.OPENAI_RESPONSES:
                return ProviderCapabilities()

    async def complete(self, request: ModelRequest) -> ModelOutput:
        # 在一次完整请求期间持有锁，使 profile 切换成为明确的 ModelCall 间操作，
        # 而不是请求途中的状态变更。
        async with self._lock:
            if self._provider is None:
                self._provider = self._factory(self._connection)
            return await self._provider.complete(request)

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
        """在完整 SSE 响应期间保持同一个适配器和连接。"""

        async with self._lock:
            if self._provider is None:
                self._provider = self._factory(self._connection)
            provider = self._provider
            if isinstance(provider, _StreamingProvider):
                async for event in provider.stream(request):
                    yield event
                return
            async for event in CompleteModelCallAdapter(provider).stream(request):
                yield event

    async def switch(self, connection: ProviderConnection) -> None:
        async with self._lock:
            previous = self._provider
            self._provider = None
            self._connection = connection
            if isinstance(previous, _ClosableProvider):
                await previous.close()

    async def close(self) -> None:
        async with self._lock:
            if isinstance(self._provider, _ClosableProvider):
                await self._provider.close()
            self._provider = None


def _build_provider(connection: ProviderConnection) -> ModelCompletionPort:
    match connection.protocol:
        case ProviderProtocol.ANTHROPIC_MESSAGES:
            return AnthropicProvider(
                model=connection.model,
                api_key=connection.api_key,
                base_url=connection.base_url,
                provider_id=connection.id,
                reasoning=connection.reasoning,
            )
        case ProviderProtocol.OPENAI_RESPONSES:
            return OpenAIResponsesProvider(
                model=connection.model,
                api_key=connection.api_key,
                base_url=connection.base_url,
                provider_id=connection.id,
                reasoning=connection.reasoning,
            )
