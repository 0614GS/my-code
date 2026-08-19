from collections.abc import AsyncIterator

import pytest

from nano_code.auth import CredentialSource
from nano_code.config import ProviderProtocol
from nano_code.model import (
    ModelOutput,
    ModelOutputCompleted,
    ModelRequest,
    ModelStreamEvent,
    ModelStreamSequencer,
    ModelTextBlock,
    ModelTextCompleted,
    ModelTextStarted,
    ProviderCapabilities,
    SystemPrompt,
    TokenUsage,
    collect_model_output,
    completed_output_payloads,
)
from nano_code.providers.router import ProviderConnection, ProviderRouter


class FakeProvider:
    capabilities = ProviderCapabilities()

    def __init__(self, provider_id: str) -> None:
        self.provider_id = provider_id
        self.closed = False

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
        output = ModelOutput(
            content=(ModelTextBlock(self.provider_id),),
            stop_reason="end_turn",
            usage=TokenUsage(input_tokens=1, output_tokens=1),
        )
        sequencer = ModelStreamSequencer()
        for payload in completed_output_payloads(output):
            yield sequencer.emit(payload)

    async def close(self) -> None:
        self.closed = True


def connection(provider_id: str) -> ProviderConnection:
    return ProviderConnection(
        id=provider_id,
        protocol=ProviderProtocol.ANTHROPIC_MESSAGES,
        model="model",
        base_url=None,
        api_key=None,
        credential_source=CredentialSource.NONE,
    )


def empty_request() -> ModelRequest:
    return ModelRequest(
        system_prompt=SystemPrompt.from_text("system"),
        messages=(),
        tools=(),
        max_output_tokens=10,
    )


@pytest.mark.asyncio
async def test_router_switches_adapter_and_closes_previous() -> None:
    built: list[FakeProvider] = []

    def factory(value: ProviderConnection) -> FakeProvider:
        provider = FakeProvider(value.id)
        built.append(provider)
        return provider

    router = ProviderRouter(connection("first"), factory=factory)

    first = await collect_model_output(router, empty_request())
    await router.switch(connection("second"))
    second = await collect_model_output(router, empty_request())

    assert first.content == (ModelTextBlock("first"),)
    assert second.content == (ModelTextBlock("second"),)
    assert built[0].closed is True
    assert router.connection.id == "second"


@pytest.mark.asyncio
async def test_router_forwards_provider_stream_and_final_event() -> None:
    router = ProviderRouter(
        connection("complete-only"), factory=lambda _: FakeProvider("ok")
    )

    events = [event async for event in router.stream(empty_request())]

    assert [event.sequence_number for event in events] == [0, 1, 2]
    assert isinstance(events[0].payload, ModelTextStarted)
    assert isinstance(events[1].payload, ModelTextCompleted)
    assert isinstance(events[2].payload, ModelOutputCompleted)
