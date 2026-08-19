from types import SimpleNamespace
from typing import Any, cast

import pytest

from my_code.config.providers import ReasoningConfig
from my_code.model.capabilities import ProviderCapabilities
from my_code.model.events import (
    ModelOutputCompleted,
    ModelReasoningCompleted,
    ModelReasoningStarted,
    ModelTextCompleted,
    ModelTextDelta,
    ModelTextStarted,
)
from my_code.model.primitives import (
    ProviderBinding,
    ProviderContinuationState,
    ReasoningPresentation,
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
    PromptStability,
    ResolvedPromptSection,
    SystemPrompt,
    ToolOutput,
    ToolOutputDocument,
    ToolOutputImage,
    ToolOutputs,
    ToolOutputText,
    UserInput,
)
from my_code.providers.anthropic import AnthropicProvider, _system_prompt_param


def prompt() -> SystemPrompt:
    return SystemPrompt(
        (
            ResolvedPromptSection("core-a", "a", PromptStability.STATIC),
            ResolvedPromptSection("core-b", "b", PromptStability.STATIC),
            ResolvedPromptSection("environment", "cwd", PromptStability.SESSION),
            ResolvedPromptSection("request", "now", PromptStability.REQUEST),
        )
    )


def test_official_anthropic_declares_prompt_cache_capability() -> None:
    capabilities = AnthropicProvider.capabilities_for(None)

    assert capabilities.prompt_caching is True
    assert capabilities.max_prompt_cache_breakpoints == 2


def test_compatible_endpoint_conservatively_uses_plain_system_text() -> None:
    actual = _system_prompt_param(
        prompt(),
        AnthropicProvider.capabilities_for("https://gateway.example/anthropic"),
    )

    assert actual == prompt().text


def test_anthropic_cache_breakpoints_end_static_and_session_prefixes() -> None:
    actual = _system_prompt_param(
        prompt(),
        ProviderCapabilities(
            system_prompt_blocks=True,
            prompt_caching=True,
            max_prompt_cache_breakpoints=2,
        ),
    )

    blocks = cast(list[dict[str, object]], actual)
    assert "cache_control" not in blocks[0]
    assert blocks[1]["cache_control"] == {"type": "ephemeral"}
    assert blocks[2]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in blocks[3]


def test_user_context_and_attachments_surround_conversation_messages() -> None:
    user_context = UserInput((InputText("user context"),))
    history = AssistantOutput((ModelTextBlock("history"),))
    attachment = UserInput((InputText("attachment"),))
    request = ModelRequest(
        system_prompt=SystemPrompt.from_text("system"),
        input=(user_context, history, attachment),
        tools=(),
        max_output_tokens=10,
    )

    normalized = cast(list[dict[str, Any]], AnthropicProvider._messages(request.input))

    assert normalized[0]["content"][0]["text"] == "user context"
    assert normalized[1]["content"][0]["text"] == "history"
    assert normalized[2]["content"][0]["text"] == "attachment"


def test_anthropic_tool_sequence_snapshot_uses_assistant_then_user_roles() -> None:
    """Lock the phase-0 wire baseline; the user role is adapter-only encoding."""

    messages = (
        AssistantOutput((ModelToolUseBlock("call", "Read", {"path": "x"}),)),
        ToolOutputs((ToolOutput("call", (ToolOutputText("ok"),), True),)),
    )

    actual = cast(list[dict[str, Any]], AnthropicProvider._messages(messages))

    assert actual == [
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "call",
                    "name": "Read",
                    "input": {"path": "x"},
                }
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "call",
                    "content": "ok",
                    "is_error": True,
                }
            ],
        },
    ]


def test_anthropic_groups_adjacent_roles_and_preserves_multiple_results() -> None:
    items = (
        UserInput((InputText("context"),)),
        UserInput((InputText("prompt"),)),
        AssistantOutput(
            (
                ModelToolUseBlock("first", "Read", {"path": "a"}),
                ModelToolUseBlock("second", "Read", {"path": "b"}),
            )
        ),
        ToolOutputs(
            (
                ToolOutput("first", (ToolOutputText("a"),)),
                ToolOutput("second", (ToolOutputText("bad"),), True),
            )
        ),
        UserInput((InputText("reminder"),)),
    )

    actual = cast(list[dict[str, Any]], AnthropicProvider._messages(items))

    assert [message["role"] for message in actual] == [
        "user",
        "assistant",
        "user",
    ]
    assert [block["type"] for block in actual[0]["content"]] == ["text", "text"]
    assert [block["tool_use_id"] for block in actual[2]["content"][:2]] == [
        "first",
        "second",
    ]
    assert actual[2]["content"][1]["is_error"] is True
    assert actual[2]["content"][2] == {"type": "text", "text": "reminder"}


def test_anthropic_maps_image_and_document_inside_adapter() -> None:
    item = UserInput(
        (
            InputImage("image/png", "aW1hZ2U="),
            InputDocument("application/pdf", "ZG9j", "notes.pdf"),
        )
    )

    actual = cast(list[dict[str, Any]], AnthropicProvider._messages((item,)))

    assert actual == [
        {
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": "aW1hZ2U=",
                    },
                },
                {
                    "type": "document",
                    "source": {
                        "type": "base64",
                        "media_type": "application/pdf",
                        "data": "ZG9j",
                    },
                    "title": "notes.pdf",
                },
            ],
        }
    ]


def test_anthropic_maps_multimodal_tool_result_content() -> None:
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
                    is_error=True,
                ),
            )
        ),
    )

    actual = cast(list[dict[str, Any]], AnthropicProvider._messages(items))
    result = actual[1]["content"][0]

    assert result["is_error"] is True
    assert result["content"] == [
        {"type": "text", "text": "caption"},
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": "aW1hZ2U=",
            },
        },
        {
            "type": "document",
            "source": {
                "type": "base64",
                "media_type": "application/pdf",
                "data": "ZG9j",
            },
            "title": "notes.pdf",
        },
    ]


def test_anthropic_thinking_round_trips_only_for_matching_model() -> None:
    binding = ProviderBinding("anthropic-messages", "anthropic", "claude-test")
    thinking = ModelReasoningBlock(
        "thinking",
        ReasoningPresentation("verbatim", ("hidden",)),
        ProviderContinuationState(
            binding,
            "active_trajectory",
            {"type": "thinking", "thinking": "hidden", "signature": "signed"},
        ),
    )
    redacted = ModelReasoningBlock(
        "redacted",
        ReasoningPresentation("redacted"),
        ProviderContinuationState(
            binding,
            "active_trajectory",
            {"type": "redacted_thinking", "data": "ciphertext"},
        ),
    )
    message = AssistantOutput(
        (thinking, redacted, ModelToolUseBlock("call", "Read", {"path": "x"}))
    )

    matching = cast(
        list[dict[str, Any]],
        AnthropicProvider._messages((message,), model="claude-test"),
    )
    mismatched = cast(
        list[dict[str, Any]],
        AnthropicProvider._messages((message,), model="other-model"),
    )

    assert thinking.continuation is not None
    assert redacted.continuation is not None
    assert matching[0]["content"][:2] == [
        thinking.continuation.payload,
        redacted.continuation.payload,
    ]
    assert [block["type"] for block in mismatched[0]["content"]] == ["tool_use"]


def test_anthropic_response_preserves_thinking_block_order() -> None:
    provider = object.__new__(AnthropicProvider)
    provider.model = "claude-test"
    provider.binding = ProviderBinding("anthropic-messages", "anthropic", "claude-test")
    response = SimpleNamespace(
        id="message",
        content=[
            SimpleNamespace(type="thinking", thinking="hidden", signature="signed"),
            SimpleNamespace(type="text", text="working"),
            SimpleNamespace(type="redacted_thinking", data="ciphertext"),
            SimpleNamespace(type="tool_use", id="call", name="Read", input={}),
        ],
        stop_reason="tool_use",
        usage=SimpleNamespace(
            input_tokens=10,
            output_tokens=5,
            cache_creation_input_tokens=None,
            cache_read_input_tokens=None,
        ),
    )

    output = provider._response(response)  # type: ignore[arg-type]

    assert [block.type for block in output.content] == [
        "reasoning",
        "text",
        "reasoning",
        "tool_use",
    ]
    continuation = cast(ModelReasoningBlock, output.content[0]).continuation
    assert continuation is not None
    assert continuation.payload == {
        "type": "thinking",
        "thinking": "hidden",
        "signature": "signed",
    }


def test_anthropic_empty_thinking_is_hidden_but_continuation_is_preserved() -> None:
    provider = object.__new__(AnthropicProvider)
    provider.model = "deepseek-v4-flash"
    provider.binding = ProviderBinding(
        "anthropic-messages", "deepseek", "deepseek-v4-flash"
    )
    response = SimpleNamespace(
        id="message",
        content=[
            SimpleNamespace(type="thinking", thinking="", signature="signed"),
            SimpleNamespace(type="text", text="done"),
        ],
        stop_reason="end_turn",
        usage=SimpleNamespace(
            input_tokens=10,
            output_tokens=5,
            cache_creation_input_tokens=None,
            cache_read_input_tokens=None,
        ),
    )

    output = provider._response(response)  # type: ignore[arg-type]

    reasoning = cast(ModelReasoningBlock, output.content[0])
    assert reasoning.presentation == ReasoningPresentation("hidden")
    assert reasoning.continuation is not None
    assert reasoning.continuation.payload == {
        "type": "thinking",
        "thinking": "",
        "signature": "signed",
    }


@pytest.mark.asyncio
async def test_anthropic_stream_empty_thinking_completes_hidden_without_replay() -> (
    None
):
    provider = object.__new__(AnthropicProvider)
    provider.model = "deepseek-v4-flash"
    provider.binding = ProviderBinding(
        "anthropic-messages", "deepseek", "deepseek-v4-flash"
    )
    provider.reasoning = ReasoningConfig(enabled=False)
    provider._capabilities = ProviderCapabilities()
    final = SimpleNamespace(
        id="message",
        content=[
            SimpleNamespace(type="thinking", thinking="", signature="signed"),
            SimpleNamespace(type="text", text="done"),
        ],
        stop_reason="end_turn",
        usage=SimpleNamespace(
            input_tokens=10,
            output_tokens=5,
            cache_creation_input_tokens=None,
            cache_read_input_tokens=None,
        ),
    )
    raw_events = (
        SimpleNamespace(
            type="content_block_start",
            index=0,
            content_block=SimpleNamespace(type="thinking", thinking=""),
        ),
        SimpleNamespace(type="content_block_stop", index=0),
        SimpleNamespace(
            type="content_block_start",
            index=1,
            content_block=SimpleNamespace(type="text", text=""),
        ),
        SimpleNamespace(
            type="content_block_delta",
            index=1,
            delta=SimpleNamespace(type="text_delta", text="done"),
        ),
        SimpleNamespace(type="content_block_stop", index=1),
    )

    class Stream:
        async def __aenter__(self) -> "Stream":
            self._events = iter(raw_events)
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        def __aiter__(self) -> "Stream":
            return self

        async def __anext__(self) -> object:
            try:
                return next(self._events)
            except StopIteration as error:
                raise StopAsyncIteration from error

        async def get_final_message(self) -> object:
            return final

    class Messages:
        def stream(self, **_: object) -> Stream:
            return Stream()

    provider.client = SimpleNamespace(messages=Messages())  # type: ignore[assignment]
    request = ModelRequest(
        SystemPrompt.from_text("system"),
        (UserInput((InputText("hello"),)),),
        (),
        100,
    )

    events = [event async for event in provider.stream(request)]
    payloads = [event.payload for event in events]

    assert [event.sequence_number for event in events] == list(range(len(events)))
    assert [type(payload) for payload in payloads[:-1]] == [
        ModelReasoningStarted,
        ModelReasoningCompleted,
        ModelTextStarted,
        ModelTextDelta,
        ModelTextCompleted,
    ]
    assert isinstance(payloads[-1], ModelOutputCompleted)
    assert payloads[-1].output.content[-1] == ModelTextBlock("done")
    completed = cast(ModelReasoningCompleted, payloads[1])
    assert completed.presentation == ReasoningPresentation("hidden")
