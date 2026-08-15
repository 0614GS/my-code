import pytest

from nano_code.agent import ContextPlan, ModelResponseCompleted
from nano_code.auth import CredentialSource
from nano_code.messages import ModelResponse, TextBlock, TokenUsage
from nano_code.prompts import SystemPrompt
from nano_code.providers import ProviderCapabilities
from nano_code.providers.profiles import ProviderProtocol
from nano_code.providers.router import ProviderConnection, ProviderRouter


class FakeProvider:
    capabilities = ProviderCapabilities()

    def __init__(self, provider_id: str) -> None:
        self.provider_id = provider_id
        self.closed = False

    async def complete(self, request: ContextPlan) -> ModelResponse:
        return ModelResponse(
            content=(TextBlock(self.provider_id),),
            stop_reason="end_turn",
            usage=TokenUsage(input_tokens=1, output_tokens=1),
        )

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


def empty_request() -> ContextPlan:
    return ContextPlan(
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

    first = await router.complete(empty_request())
    await router.switch(connection("second"))
    second = await router.complete(empty_request())

    assert first.content == (TextBlock("first"),)
    assert second.content == (TextBlock("second"),)
    assert built[0].closed is True
    assert router.connection.id == "second"


@pytest.mark.asyncio
async def test_router_adapts_complete_only_provider_to_final_stream_event() -> None:
    router = ProviderRouter(
        connection("complete-only"), factory=lambda _: FakeProvider("ok")
    )

    events = [event async for event in router.stream(empty_request())]

    assert len(events) == 1
    assert isinstance(events[0], ModelResponseCompleted)
