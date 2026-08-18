from types import SimpleNamespace

from nano_code.agent import (
    ModelAssistantMessage,
    ModelReasoningBlock,
    ModelRequest,
    ModelTextBlock,
    ModelToolResultBlock,
    ModelToolUseBlock,
    ModelUserMessage,
)
from nano_code.conversation import ProviderBinding, ProviderContinuationState
from nano_code.prompts import SystemPrompt
from nano_code.providers.openai_responses import OpenAIResponsesProvider
from nano_code.providers.profiles import ReasoningConfig


class Item:
    def __init__(self, value: dict[str, object]) -> None:
        self.value = value

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
    raw = {
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
