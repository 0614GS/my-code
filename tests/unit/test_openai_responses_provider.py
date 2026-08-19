from types import SimpleNamespace
from typing import cast

import pytest

from my_code.config.providers import ReasoningConfig
from my_code.model.events import (
    ModelOutputCompleted,
    ModelReasoningCompleted,
    ModelReasoningDelta,
    ModelReasoningStarted,
    ModelTextCompleted,
    ModelTextDelta,
    ModelTextStarted,
)
from my_code.model.primitives import (
    JsonObject,
    ProviderBinding,
    ProviderContinuationState,
)
from my_code.model.request import (
    AssistantOutput,
    InputDocument,
    InputImage,
    InputText,
    ModelReasoningBlock,
    ModelRequest,
    ModelTextBlock,
    ModelToolUseBlock,
    SystemPrompt,
    ToolOutput,
    ToolOutputDocument,
    ToolOutputImage,
    ToolOutputs,
    ToolOutputText,
    UserInput,
)
from my_code.providers.openai_responses import (
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


def test_openai_tool_sequence_snapshot_uses_top_level_function_items() -> None:
    """Lock the phase-0 wire baseline: tool output is not a user message."""

    provider = _provider()
    messages = (
        AssistantOutput((ModelToolUseBlock("call", "Read", {"path": "x"}),)),
        ToolOutputs((ToolOutput("call", (ToolOutputText("ok"),)),)),
    )

    assert provider._input(messages) == [
        {
            "type": "function_call",
            "call_id": "call",
            "name": "Read",
            "arguments": '{"path":"x"}',
        },
        {
            "type": "function_call_output",
            "call_id": "call",
            "output": "ok",
        },
    ]


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
        AssistantOutput(
            (ModelToolUseBlock("call", "Read", {"path": "x"}, continuation),)
        ),
        ToolOutputs((ToolOutput("call", (ToolOutputText("ok"),)),)),
    )

    result = provider._input(messages)

    assert result[0] == raw
    assert result[1] == {
        "type": "function_call_output",
        "call_id": "call",
        "output": "ok",
    }


def test_openai_preserves_multiple_outputs_and_stably_encodes_errors() -> None:
    provider = _provider()
    items = (
        AssistantOutput(
            (
                ModelToolUseBlock("first", "Read", {"path": "a"}),
                ModelToolUseBlock("second", "Read", {"path": "b"}),
            )
        ),
        ToolOutputs(
            (
                ToolOutput("first", (ToolOutputText("a"),)),
                ToolOutput("second", (ToolOutputText("denied"),), True),
            )
        ),
    )

    result = cast(list[dict[str, object]], provider._input(items))

    assert [item["call_id"] for item in result] == [
        "first",
        "second",
        "first",
        "second",
    ]
    assert result[-2]["output"] == "a"
    assert result[-1]["output"] == "Error: denied"


def test_openai_maps_multimodal_user_input_without_provider_types_in_core() -> None:
    provider = _provider()
    item = UserInput(
        (
            InputText("inspect"),
            InputImage("image/png", "aW1hZ2U="),
            InputDocument("application/pdf", "ZG9j", "notes.pdf"),
        )
    )

    assert provider._input((item,)) == [
        {
            "role": "user",
            "content": [
                {"type": "input_text", "text": "inspect"},
                {
                    "type": "input_image",
                    "image_url": "data:image/png;base64,aW1hZ2U=",
                    "detail": "auto",
                },
                {
                    "type": "input_file",
                    "file_data": "data:application/pdf;base64,ZG9j",
                    "filename": "notes.pdf",
                },
            ],
        }
    ]


def test_openai_maps_multimodal_tool_output_without_role_wrapper() -> None:
    provider = _provider()
    items = (
        AssistantOutput((ModelToolUseBlock("call", "Read", {}),)),
        ToolOutputs(
            (
                ToolOutput(
                    "call",
                    (
                        ToolOutputText("caption"),
                        ToolOutputImage("image/png", "aW1hZ2U="),
                        ToolOutputDocument("application/pdf", "ZG9j", "notes.pdf"),
                    ),
                ),
            )
        ),
    )

    output = provider._input(items)[1]

    assert output == {
        "type": "function_call_output",
        "call_id": "call",
        "output": [
            {"type": "input_text", "text": "caption"},
            {
                "type": "input_image",
                "image_url": "data:image/png;base64,aW1hZ2U=",
                "detail": "auto",
            },
            {
                "type": "input_file",
                "file_data": "data:application/pdf;base64,ZG9j",
                "filename": "notes.pdf",
            },
        ],
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
    result = provider._input((AssistantOutput((ModelTextBlock("visible", other),)),))
    assert result == [{"role": "assistant", "content": "visible"}]


def test_request_is_stateless_and_requests_safe_reasoning_summary() -> None:
    provider = _provider()
    request = ModelRequest(
        SystemPrompt.from_text("system"),
        (UserInput((InputText("hello"),)),),
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
async def test_openai_stream_emits_final_snapshot_after_ephemeral_deltas() -> None:
    provider = _provider()
    final = SimpleNamespace(
        status="completed",
        output=[
            Item(
                {
                    "type": "message",
                    "id": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "final"}],
                }
            )
        ],
        usage=SimpleNamespace(
            input_tokens=3,
            output_tokens=1,
            input_tokens_details=SimpleNamespace(cached_tokens=0),
        ),
    )
    raw_events = (
        SimpleNamespace(
            type="response.output_text.delta",
            sequence_number=1,
            output_index=0,
            content_index=0,
            delta="draft",
        ),
        SimpleNamespace(
            type="response.completed",
            sequence_number=2,
            response=final,
        ),
    )

    class EventStream:
        def __aiter__(self) -> "EventStream":
            self._events = iter(raw_events)
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
        (UserInput((InputText("hello"),)),),
        (),
        100,
    )

    events = [event async for event in provider.stream(request)]
    payloads = [event.payload for event in events]

    completed = payloads[-1]
    assert isinstance(completed, ModelOutputCompleted)
    assert len(completed.output.content) == 1
    final_text = completed.output.content[0]
    assert isinstance(final_text, ModelTextBlock)
    assert final_text.text == "final"


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
        (UserInput((InputText("hello"),)),),
        (),
        100,
    )

    with pytest.raises(RuntimeError, match="sequence numbers"):
        _ = [event async for event in provider.stream(request)]
