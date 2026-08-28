"""用于在 ModelCall 之间原子切换 provider 连接的运行时路由器。"""

import asyncio
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from my_code.auth.credentials import CredentialSource
from my_code.config.providers import CompactConfig, ProviderProtocol, ReasoningConfig
from my_code.model.capabilities import ModelLimits, ProviderCapabilities
from my_code.model.client import ModelClient
from my_code.model.events import ModelStreamEvent
from my_code.model.primitives import ProviderBinding
from my_code.model.request import ModelRequest
from my_code.providers.capabilities import capabilities_for


@dataclass(frozen=True, slots=True)
class ProviderConnection:
    """已完整解析、可用于构造 SDK 适配器的 profile。"""

    id: str
    protocol: ProviderProtocol
    model: str
    base_url: str | None
    api_key: str | None = field(repr=False)
    credential_source: CredentialSource
    reasoning: ReasoningConfig = ReasoningConfig()
    limits: ModelLimits = ModelLimits()
    compact: CompactConfig = CompactConfig()


type ProviderFactory = Callable[[ProviderConnection], ModelClient]


@runtime_checkable
class _ClosableProvider(Protocol):
    async def close(self) -> None:
        """释放 provider 持有的网络资源。"""


class ProviderRouter(ModelClient):
    """串行化请求与切换，同时保持智能体循环协议。"""

    def __init__(
        self,
        connection: ProviderConnection,
        *,
        factory: ProviderFactory | None = None,
    ) -> None:
        self._factory = factory or _build_provider
        self._connection = connection
        self._provider: ModelClient | None = None
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

        return _capabilities_for(self._connection)

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
        """在完整 SSE 响应期间保持同一个适配器和连接。"""

        async with self._lock:
            if self._provider is None:
                self._provider = self._factory(self._connection)
            provider = self._provider
            async for event in provider.stream(request):
                yield event

    async def switch(self, connection: ProviderConnection) -> None:
        async with self._lock:
            previous = self._provider
            if isinstance(previous, _ClosableProvider):
                await previous.close()
            self._provider = None
            self._connection = connection

    async def close(self) -> None:
        async with self._lock:
            if isinstance(self._provider, _ClosableProvider):
                await self._provider.close()
            self._provider = None


def _build_provider(connection: ProviderConnection) -> ModelClient:
    match connection.protocol:
        case ProviderProtocol.ANTHROPIC_MESSAGES:
            from my_code.providers.anthropic import AnthropicProvider

            return AnthropicProvider(
                model=connection.model,
                api_key=connection.api_key,
                base_url=connection.base_url,
                provider_id=connection.id,
                reasoning=connection.reasoning,
            )
        case ProviderProtocol.OPENAI_RESPONSES:
            from my_code.providers.openai_responses import OpenAIResponsesProvider

            return OpenAIResponsesProvider(
                model=connection.model,
                api_key=connection.api_key,
                base_url=connection.base_url,
                provider_id=connection.id,
                reasoning=connection.reasoning,
            )


def _capabilities_for(connection: ProviderConnection) -> ProviderCapabilities:
    return capabilities_for(connection.protocol, connection.base_url)


__all__ = [
    "ProviderConnection",
    "ProviderFactory",
    "ProviderRouter",
]
