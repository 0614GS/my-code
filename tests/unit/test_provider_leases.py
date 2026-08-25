"""Captured provider binding and independent client lease tests."""

import asyncio
from collections.abc import AsyncIterator

import pytest

from my_code.application.state import ProviderRuntime
from my_code.auth.credentials import CredentialSource
from my_code.config.providers import ProviderProtocol
from my_code.model.capabilities import fallback_descriptor, resolve_environment
from my_code.model.client import collect_model_output
from my_code.model.events import (
    ModelOutputCompleted,
    ModelStreamEvent,
    ModelStreamSequencer,
)
from my_code.model.primitives import TokenUsage
from my_code.model.request import (
    ModelOutput,
    ModelRequest,
    ModelTextBlock,
    SystemPrompt,
)
from my_code.providers.leases import ProviderLeaseRegistry
from my_code.providers.router import ProviderConnection, ProviderRouter


class FakeProvider:
    def __init__(self, provider_id: str) -> None:
        self.provider_id = provider_id
        self.closed = False

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
        del request
        output = ModelOutput(
            (ModelTextBlock(self.provider_id),),
            "end_turn",
            TokenUsage(1, 1),
        )
        yield ModelStreamSequencer().emit(ModelOutputCompleted(output))

    async def close(self) -> None:
        self.closed = True


def connection(provider_id: str) -> ProviderConnection:
    return ProviderConnection(
        id=provider_id,
        protocol=ProviderProtocol.ANTHROPIC_MESSAGES,
        model=f"{provider_id}-model",
        base_url=None,
        api_key=None,
        credential_source=CredentialSource.NONE,
    )


def request() -> ModelRequest:
    return ModelRequest(SystemPrompt.from_text("system"), (), (), 10)


@pytest.mark.asyncio
async def test_switch_only_changes_leases_acquired_after_switch() -> None:
    built: list[FakeProvider] = []

    def factory(value: ProviderConnection) -> FakeProvider:
        provider = FakeProvider(value.id)
        built.append(provider)
        return provider

    first_connection = connection("first")
    registry = ProviderLeaseRegistry(first_connection, factory=factory)
    router = ProviderRouter(first_connection, factory=factory)
    first_environment = resolve_environment(
        fallback_descriptor("first-model"),
        requested_output_tokens=10,
        configured_trigger_tokens=None,
    )
    runtime = ProviderRuntime(router, registry, first_environment)
    first = registry.acquire()
    second_connection = connection("second")
    second_environment = resolve_environment(
        fallback_descriptor("second-model"),
        requested_output_tokens=10,
        configured_trigger_tokens=None,
    )

    await runtime.switch(second_connection, second_environment)
    second = registry.acquire()

    assert first.binding.provider_id == "first"
    assert first.binding.model == "first-model"
    assert second.binding.provider_id == "second"
    assert second.binding.model == "second-model"
    assert (await collect_model_output(first, request())).content == (
        ModelTextBlock("first"),
    )
    assert (await collect_model_output(second, request())).content == (
        ModelTextBlock("second"),
    )
    await runtime.close()
    assert all(provider.closed for provider in built)


@pytest.mark.asyncio
async def test_registry_close_releases_all_leases_and_rejects_acquire() -> None:
    built: list[FakeProvider] = []

    def factory(value: ProviderConnection) -> FakeProvider:
        provider = FakeProvider(value.id)
        built.append(provider)
        return provider

    registry = ProviderLeaseRegistry(connection("active"), factory=factory)
    first = registry.acquire()
    second = registry.acquire()
    await asyncio.gather(
        collect_model_output(first, request()),
        collect_model_output(second, request()),
    )

    await registry.close()

    assert registry.active_count == 0
    assert first.closed and second.closed
    assert all(provider.closed for provider in built)
    with pytest.raises(RuntimeError, match="closed"):
        registry.acquire()
