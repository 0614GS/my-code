from types import SimpleNamespace

import pytest

from nano_code.config import ReasoningConfig
from nano_code.model import (
    JsonObject,
    ModelAssistantMessage,
    ModelReasoningBlock,
    ModelReasoningCompleted,
    ModelReasoningDelta,
    ModelReasoningStarted,
    ModelRequest,
    ModelTextBlock,
    ModelTextCompleted,
    ModelTextDelta,
    ModelTextStarted,
    ModelToolResultBlock,
    ModelToolUseBlock,
    ModelUserMessage,
    ProviderBinding,
    ProviderContinuationState,
    SystemPrompt,
)
from nano_code.providers.openai_responses import (
    OpenAIResponsesProvider,
    _OpenAIStreamNormalizer,
)


class Item:
    def __init__(self, value: dict[str, object]) -> None:
        self.value = value
        self.type = value.get("type")

    def model_dump(self, **_: object) -> dict[str, object]:
        return self.value


def _provider() -> OpenAIResponsesProvider:
    provider = object.__new__(OpenAIResponsesProvider)
    provider.model = "gpt-test"
    provider.binding = ProviderBinding("openai-responses", "openai", "gpt-test", None)
    provider.reasoning = ReasoningConfig(True, "high", "auto")
    return provider


def test_response_preserves_reasoning_and_output_item_order() -> None:
    provider = _provider()
    response = SimpleNamespace(
        id="resp",
        status="completed",
        output=[
            Item(
                {
                    "type": "reasoning",
                    "id": "reasoning",
                    "encrypted_content": "ciphertext",
                    "summary": [{"type": "summary_text", "text": "Safe summary"}],
                    "status": "completed",
                }
            ),
            Item(
                {
                    "type": "function_call",
                    "id": "fc",
                    "call_id": "call",
                    "name": "Read",
                    "arguments": '{"path":"x"}',
                    "status": "completed",
                }
            ),
            Item(
                {
                    "type": "message",
                    "id": "msg",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": "done"}],
                }
            ),
        ],
        usage=SimpleNamespace(
            input_tokens=12,
            output_tokens=3,
            input_tokens_details=SimpleNamespace(cached_tokens=2),
        ),
    )

    output = provider._response(response)

    assert [block.type for block in output.content] == [
        "reasoning",
        "tool_use",
        "text",
    ]
    reasoning = output.content[0]
    assert isinstance(reasoning, ModelReasoningBlock)
    assert reasoning.presentation.parts == ("Safe summary",)
    assert reasoning.continuation is not None
    assert reasoning.continuation.payload["encrypted_content"] == "ciphertext"
    assert output.usage.total_input_tokens == 12


def test_input_replays_matching_items_and_maps_tool_results() -> None:
    provider = _provider()
    raw: JsonObject = {
        "type": "function_call",
        "id": "fc",
        "call_id": "call",
        "name": "Read",
        "arguments": '{"path":"x"}',
        "status": "completed",
    }
    continuation = ProviderContinuationState(provider.binding, "working_context", raw)
    messages = (
        ModelAssistantMessage(
            (ModelToolUseBlock("call", "Read", {"path": "x"}, continuation),)
        ),
        ModelUserMessage((ModelToolResultBlock("call", "ok"),)),
    )

    result = provider._input(messages)

    assert result[0] == raw
    assert result[1] == {
        "type": "function_call_output",
        "call_id": "call",
        "output": "ok",
    }


def test_mismatched_binding_does_not_replay_encrypted_item() -> None:
    provider = _provider()
    other = ProviderContinuationState(
        ProviderBinding("openai-responses", "other", "gpt-test"),
        "working_context",
        {
            "type": "message",
            "id": "msg",
            "role": "assistant",
            "content": [],
            "encrypted_content": "secret",
        },
    )
    result = provider._input(
        (ModelAssistantMessage((ModelTextBlock("visible", other),)),)
    )
    assert result == [{"role": "assistant", "content": "visible"}]


def test_request_is_stateless_and_requests_safe_reasoning_summary() -> None:
    provider = _provider()
    request = ModelRequest(
        SystemPrompt.from_text("system"),
        (ModelUserMessage((ModelTextBlock("hello"),)),),
        (),
        100,
    )

    params = provider._request_params(request)

    assert params["store"] is False
    assert "previous_response_id" not in params
    assert params["reasoning"] == {
        "summary": "auto",
        "context": "auto",
        "effort": "high",
    }
    assert params["include"] == ["reasoning.encrypted_content"]


def test_stream_normalizer_serializes_interleaved_output_items() -> None:
    normalizer = _OpenAIStreamNormalizer()
    reasoning_delta = SimpleNamespace(
        type="response.reasoning_summary_text.delta",
        output_index=0,
        summary_index=0,
        delta="Safe ",
    )
    text_delta = SimpleNamespace(
        type="response.output_text.delta",
        output_index=1,
        content_index=0,
        delta="draft",
    )
    reasoning_done = SimpleNamespace(
        type="response.output_item.done",
        output_index=0,
        item=Item(
            {
                "type": "reasoning",
                "id": "reasoning",
                "summary": [{"type": "summary_text", "text": "Safe summary"}],
            }
        ),
    )
    text_done = SimpleNamespace(
        type="response.output_item.done",
        output_index=1,
        item=Item(
            {
                "type": "message",
                "id": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "final"}],
            }
        ),
    )

    payloads = normalizer.feed(reasoning_delta)
    assert [type(payload) for payload in payloads] == [
        ModelReasoningStarted,
        ModelReasoningDelta,
    ]
    assert normalizer.feed(text_delta) == []

    payloads = normalizer.feed(reasoning_done)
    assert [type(payload) for payload in payloads] == [
        ModelReasoningCompleted,
        ModelTextStarted,
        ModelTextDelta,
    ]
    payloads = normalizer.feed(text_done)
    assert [type(payload) for payload in payloads] == [ModelTextCompleted]
    assert payloads[0].text == "final"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_openai_stream_rejects_non_increasing_provider_sequence() -> None:
    provider = _provider()

    class EventStream:
        def __aiter__(self) -> "EventStream":
            self._events = iter(
                (
                    SimpleNamespace(type="response.created", sequence_number=2),
                    SimpleNamespace(type="response.in_progress", sequence_number=1),
                )
            )
            return self

        async def __anext__(self) -> object:
            try:
                return next(self._events)
            except StopIteration as error:
                raise StopAsyncIteration from error

    class Responses:
        async def create(self, **_: object) -> EventStream:
            return EventStream()

    provider.client = SimpleNamespace(responses=Responses())  # type: ignore[assignment]
    request = ModelRequest(
        SystemPrompt.from_text("system"),
        (ModelUserMessage((ModelTextBlock("hello"),)),),
        (),
        100,
    )

    with pytest.raises(RuntimeError, match="sequence numbers"):
        _ = [event async for event in provider.stream(request)]
