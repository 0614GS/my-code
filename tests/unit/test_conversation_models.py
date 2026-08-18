"""Conversation fact model tests."""

import pytest

from nano_code.agent import (
    ModelAssistantMessage,
    ModelTextBlock,
    ModelToolResultBlock,
    ModelToolUseBlock,
    ModelUserMessage,
)
from nano_code.conversation import (
    AssistantMessage,
    ConversationSummaryMessage,
    HumanMessage,
    TextContent,
    TokenUsage,
    ToolCall,
    ToolResult,
    ToolResultsMessage,
)


def test_conversation_union_has_four_semantic_variants() -> None:
    human = HumanMessage("hello")
    assistant = AssistantMessage((TextContent("answer"),), TokenUsage())
    results = ToolResultsMessage((ToolResult("call", "value"),), assistant.uuid)
    summary = ConversationSummaryMessage("state")

    assert (human.kind, assistant.kind, results.kind, summary.kind) == (
        "human",
        "assistant",
        "tool_results",
        "conversation_summary",
    )
    assert not hasattr(summary, "role")
    assert not hasattr(summary, "origin")


def test_conversation_variants_reject_cross_layer_content() -> None:
    with pytest.raises(TypeError, match="only text and tool calls"):
        AssistantMessage((ToolResult("call", "bad"),), TokenUsage())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="only tool results"):
        ToolResultsMessage((ToolCall("call", "Read", {}),), "source")  # type: ignore[arg-type]


def test_model_role_variants_reject_opposite_role_blocks() -> None:
    with pytest.raises(TypeError, match="only text or tool results"):
        ModelUserMessage((ModelToolUseBlock("call", "Read", {}),))  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="only text or tool uses"):
        ModelAssistantMessage((ModelToolResultBlock("call", "bad"),))  # type: ignore[arg-type]

    assert ModelUserMessage((ModelTextBlock("user"),)).role == "user"
    assert ModelAssistantMessage((ModelTextBlock("assistant"),)).role == "assistant"
