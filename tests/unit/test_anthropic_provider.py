from typing import cast

from anthropic.types import TextBlockParam

from nano_code.agent import ContextPlan, ModelMessage
from nano_code.messages import TextBlock
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
            ResolvedPromptSection("turn", "now", PromptStability.TURN),
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


def test_workspace_context_is_serialized_before_conversation_messages() -> None:
    workspace = ModelMessage("user", (TextBlock("workspace facts"),))
    history = ModelMessage("assistant", (TextBlock("history"),))
    request = ContextPlan(
        system_prompt=SystemPrompt.from_text("system"),
        messages=(history,),
        tools=(),
        max_output_tokens=10,
        workspace_context=(workspace,),
    )

    projected = AnthropicProvider._request_messages(request)

    assert projected[0]["content"][0]["text"] == "workspace facts"
    assert projected[1]["content"][0]["text"] == "history"
