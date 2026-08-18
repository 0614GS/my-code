from types import SimpleNamespace
from typing import cast

from anthropic.types import TextBlockParam

from nano_code.agent import (
    ModelAssistantMessage,
    ModelOpaqueAssistantBlock,
    ModelRequest,
    ModelTextBlock,
    ModelToolUseBlock,
    ModelUserMessage,
)
from nano_code.prompts import (
    PromptStability,
    ResolvedPromptSection,
    SystemPrompt,
)
from nano_code.providers import ProviderCapabilities
from nano_code.providers.anthropic import AnthropicProvider, _system_prompt_param


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

    blocks = cast(list[TextBlockParam], actual)
    assert "cache_control" not in blocks[0]
    assert blocks[1]["cache_control"] == {"type": "ephemeral"}
    assert blocks[2]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in blocks[3]


def test_user_context_and_attachments_surround_conversation_messages() -> None:
    user_context = ModelUserMessage((ModelTextBlock("user context"),))
    history = ModelAssistantMessage((ModelTextBlock("history"),))
    attachment = ModelUserMessage((ModelTextBlock("attachment"),))
    request = ModelRequest(
        system_prompt=SystemPrompt.from_text("system"),
        messages=(user_context, history, attachment),
        tools=(),
        max_output_tokens=10,
    )

    normalized = AnthropicProvider._messages(request.messages)

    assert normalized[0]["content"][0]["text"] == "user context"
    assert normalized[1]["content"][0]["text"] == "history"
    assert normalized[2]["content"][0]["text"] == "attachment"


def test_anthropic_thinking_round_trips_only_for_matching_model() -> None:
    thinking = ModelOpaqueAssistantBlock(
        "anthropic-messages",
        "claude-test",
        {"type": "thinking", "thinking": "hidden", "signature": "signed"},
    )
    redacted = ModelOpaqueAssistantBlock(
        "anthropic-messages",
        "claude-test",
        {"type": "redacted_thinking", "data": "ciphertext"},
    )
    message = ModelAssistantMessage(
        (thinking, redacted, ModelToolUseBlock("call", "Read", {"path": "x"}))
    )

    matching = AnthropicProvider._messages((message,), model="claude-test")
    mismatched = AnthropicProvider._messages((message,), model="other-model")

    assert matching[0]["content"][:2] == [thinking.payload, redacted.payload]
    assert [block["type"] for block in mismatched[0]["content"]] == ["tool_use"]


def test_anthropic_response_preserves_thinking_block_order() -> None:
    provider = object.__new__(AnthropicProvider)
    provider.model = "claude-test"
    response = SimpleNamespace(
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
        "provider_opaque",
        "text",
        "provider_opaque",
        "tool_use",
    ]
    assert cast(ModelOpaqueAssistantBlock, output.content[0]).payload == {
        "type": "thinking",
        "thinking": "hidden",
        "signature": "signed",
    }
