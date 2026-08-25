"""Independent provider clients captured for concurrent Agent runs."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from typing import Protocol, runtime_checkable
from uuid import uuid4

from my_code.model.capabilities import ProviderCapabilities
from my_code.model.client import ModelClient
from my_code.model.events import ModelStreamEvent
from my_code.model.primitives import ProviderBinding
from my_code.model.request import ModelRequest
from my_code.providers.router import (
    ProviderConnection,
    ProviderFactory,
    _build_provider,
    _capabilities_for,
)


@runtime_checkable
class _ClosableProvider(Protocol):
    async def close(self) -> None: ...


class ProviderClientLease(ModelClient):
    """One immutable connection binding with its own client and stream lock."""

    def __init__(
        self,
        connection: ProviderConnection,
        *,
        factory: ProviderFactory,
        release: CallableRelease,
    ) -> None:
        self.lease_id = str(uuid4())
        self.connection = connection
        self._factory = factory
        self._release = release
        self._provider: ModelClient | None = None
        self._lock = asyncio.Lock()
        self._closed = False

    @property
    def binding(self) -> ProviderBinding:
        connection = self.connection
        return ProviderBinding(
            connection.protocol.value,
            connection.id,
            connection.model,
            connection.base_url,
        )

    @property
    def capabilities(self) -> ProviderCapabilities:
        return _capabilities_for(self.connection)

    @property
    def closed(self) -> bool:
        return self._closed

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
        async with self._lock:
            if self._closed:
                raise RuntimeError("Provider lease is closed")
            if self._provider is None:
                self._provider = self._factory(self.connection)
            provider = self._provider
            async for event in provider.stream(request):
                yield event

    async def close(self) -> None:
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                if isinstance(self._provider, _ClosableProvider):
                    await self._provider.close()
            finally:
                self._provider = None
                self._release(self.lease_id)


type CallableRelease = Callable[[str], None]


class ProviderLeaseRegistry:
    """Own all run leases while preserving each captured connection."""

    def __init__(
        self,
        connection: ProviderConnection,
        *,
        factory: ProviderFactory | None = None,
    ) -> None:
        self._connection = connection
        self._factory = factory or _build_provider
        self._leases: dict[str, ProviderClientLease] = {}
        self._accepting = True

    @property
    def connection(self) -> ProviderConnection:
        return self._connection

    @property
    def active_count(self) -> int:
        return len(self._leases)

    def acquire(self) -> ProviderClientLease:
        if not self._accepting:
            raise RuntimeError("Provider lease registry is closed")
        lease = ProviderClientLease(
            self._connection,
            factory=self._factory,
            release=self._release,
        )
        self._leases[lease.lease_id] = lease
        return lease

    def switch(self, connection: ProviderConnection) -> None:
        if not self._accepting:
            raise RuntimeError("Provider lease registry is closed")
        self._connection = connection

    async def close(self) -> None:
        if not self._accepting and not self._leases:
            return
        self._accepting = False
        results = await asyncio.gather(
            *(lease.close() for lease in tuple(self._leases.values())),
            return_exceptions=True,
        )
        cancellations = tuple(
            result for result in results if isinstance(result, asyncio.CancelledError)
        )
        if cancellations:
            raise cancellations[0]
        failures = tuple(result for result in results if isinstance(result, Exception))
        if failures:
            raise ExceptionGroup("Failed to close provider leases", failures)

    def _release(self, lease_id: str) -> None:
        self._leases.pop(lease_id, None)


__all__ = [
    "ProviderClientLease",
    "ProviderLeaseRegistry",
]
