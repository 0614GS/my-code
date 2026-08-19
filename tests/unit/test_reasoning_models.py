import pytest

from my_code.context.normalization import ModelInputNormalizer
from my_code.conversation.models import (
    AssistantMessage,
    HumanMessage,
    ReasoningContent,
    TextContent,
    ToolCall,
    ToolResult,
    ToolResultBatch,
)
from my_code.model.primitives import (
    ProviderBinding,
    ProviderContinuationState,
    ReasoningPresentation,
    TokenUsage,
)
from my_code.model.request import AssistantOutput, ModelOutput, ModelReasoningBlock


@pytest.mark.parametrize("disclosure", ["verbatim", "summary"])
def test_visible_reasoning_requires_parts(disclosure: str) -> None:
    with pytest.raises(ValueError, match="contain presentation"):
        ReasoningPresentation(disclosure, ())  # type: ignore[arg-type]


@pytest.mark.parametrize("disclosure", ["redacted", "hidden"])
def test_hidden_reasoning_rejects_parts(disclosure: str) -> None:
    with pytest.raises(ValueError, match="must not contain"):
        ReasoningPresentation(disclosure, ("secret",))  # type: ignore[arg-type]


def test_continuation_payload_is_defensively_copied() -> None:
    payload = {"type": "reasoning", "summary": [{"text": "safe"}]}
    state = ProviderContinuationState(
        ProviderBinding("openai-responses", "openai", "gpt-test"),
        "working_context",
        payload,  # type: ignore[arg-type]
    )
    payload["summary"] = []
    assert state.payload["summary"] == [{"text": "safe"}]


def test_provider_binding_rejects_unsafe_profile_id() -> None:
    with pytest.raises(ValueError, match="provider_id"):
        ProviderBinding("openai-responses", "../other", "gpt-test")


def test_reasoning_only_output_is_not_actionable() -> None:
    with pytest.raises(ValueError, match="no actionable"):
        ModelOutput(
            (ModelReasoningBlock("r", ReasoningPresentation("summary", ("safe",))),),
            "completed",
        )


def test_scoped_continuations_are_selected_and_compaction_strips_them() -> None:
    binding = ProviderBinding("openai-responses", "openai", "gpt-test")
    working = ProviderContinuationState(
        binding,
        "working_context",
        {"type": "message", "id": "msg", "role": "assistant", "content": []},
    )
    active = ProviderContinuationState(
        ProviderBinding("anthropic-messages", "anthropic", "claude-test"),
        "active_trajectory",
        {"type": "thinking", "thinking": "shown", "signature": "secret"},
    )
    human = HumanMessage("go")
    assistant = AssistantMessage(
        (
            ReasoningContent(
                "r",
                ReasoningPresentation("verbatim", ("shown",)),
                active,
            ),
            TextContent("calling", working),
            ToolCall("call", "Read", {"path": "x"}),
        ),
        TokenUsage(),
        parent_uuid=human.uuid,
    )
    results = ToolResultBatch(
        (ToolResult("call", "ok"),), assistant.uuid, parent_uuid=assistant.uuid
    )
    normalizer = ModelInputNormalizer()

    active_view = normalizer.normalize((), (human, assistant, results), ())
    compact_view = normalizer.normalize_transcript((human, assistant, results))
    mismatched_view = normalizer.normalize(
        (),
        (human, assistant, results),
        (),
        active_binding=ProviderBinding("openai-responses", "other", "gpt-test"),
    )

    assert any(
        isinstance(block, ModelReasoningBlock)
        for item in active_view
        if isinstance(item, AssistantOutput)
        for block in item.content
    )
    assert not any(
        isinstance(block, ModelReasoningBlock)
        for item in compact_view
        if isinstance(item, AssistantOutput)
        for block in item.content
    )
    assert all(
        getattr(block, "continuation", None) is None
        for item in compact_view
        if isinstance(item, AssistantOutput)
        for block in item.content
    )
    assert not any(
        getattr(block, "continuation", None) is not None
        for item in mismatched_view
        if isinstance(item, AssistantOutput)
        for block in item.content
    )
