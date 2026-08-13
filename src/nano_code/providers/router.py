"""Runtime router for atomically switching provider connections between turns."""

import asyncio
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from nano_code.auth import CredentialSource
from nano_code.messages import ModelResponse
from nano_code.providers.anthropic import AnthropicProvider
from nano_code.providers.base import (
    ModelProvider,
    ModelRequest,
    StreamingModelProvider,
)
from nano_code.providers.events import ModelResponseCompleted, ModelStreamEvent
from nano_code.providers.profiles import ProviderProtocol


@dataclass(frozen=True, slots=True)
class ProviderConnection:
    """A fully resolved profile ready to construct an SDK adapter."""

    id: str
    protocol: ProviderProtocol
    model: str
    base_url: str | None
    api_key: str | None
    credential_source: CredentialSource


type ProviderFactory = Callable[[ProviderConnection], ModelProvider]


@runtime_checkable
class _ClosableProvider(Protocol):
    async def close(self) -> None:
        """Release provider-owned network resources."""


class ProviderRouter:
    """Serialize requests and swaps while preserving the agent-loop protocol."""

    def __init__(
        self,
        connection: ProviderConnection,
        *,
        factory: ProviderFactory | None = None,
    ) -> None:
        self._factory = factory or _build_provider
        self._connection = connection
        self._provider: ModelProvider | None = None
        self._lock = asyncio.Lock()

    @property
    def connection(self) -> ProviderConnection:
        return self._connection

    async def complete(self, request: ModelRequest) -> ModelResponse:
        # Holding the lock for one complete request makes a profile swap an
        # explicit between-turn operation rather than a mid-request mutation.
        async with self._lock:
            if self._provider is None:
                self._provider = self._factory(self._connection)
            return await self._provider.complete(request)

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
        """Keep one adapter and connection stable for the full SSE response."""

        async with self._lock:
            if self._provider is None:
                self._provider = self._factory(self._connection)
            if isinstance(self._provider, StreamingModelProvider):
                async for event in self._provider.stream(request):
                    yield event
                return
            response = await self._provider.complete(request)
            yield ModelResponseCompleted(response)

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


def _build_provider(connection: ProviderConnection) -> ModelProvider:
    match connection.protocol:
        case ProviderProtocol.ANTHROPIC_MESSAGES:
            return AnthropicProvider(
                model=connection.model,
                api_key=connection.api_key,
                base_url=connection.base_url,
            )
