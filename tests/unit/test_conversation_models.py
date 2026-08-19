"""Conversation fact model tests."""

import pytest

from my_code.conversation.models import (
    AssistantMessage,
    ConversationSummaryMessage,
    HumanMessage,
    TextContent,
    ToolCall,
    ToolResult,
    ToolResultBatch,
)
from my_code.model.primitives import TokenUsage
from my_code.model.request import (
    AssistantOutput,
    InputText,
    ModelTextBlock,
    ModelToolUseBlock,
    ToolOutput,
    ToolOutputs,
    ToolOutputText,
    UserInput,
    validate_model_input,
)


def test_conversation_union_has_four_semantic_variants() -> None:
    human = HumanMessage("hello")
    assistant = AssistantMessage((TextContent("answer"),), TokenUsage())
    results = ToolResultBatch((ToolResult("call", "value"),), assistant.uuid)
    summary = ConversationSummaryMessage("state")

    assert (human.kind, assistant.kind, results.kind, summary.kind) == (
        "human",
        "assistant",
        "tool_result_batch",
        "conversation_summary",
    )
    assert not hasattr(summary, "role")
    assert not hasattr(summary, "origin")


def test_conversation_variants_reject_cross_layer_content() -> None:
    with pytest.raises(TypeError, match="only text and tool calls"):
        AssistantMessage((ToolResult("call", "bad"),), TokenUsage())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="only tool results"):
        ToolResultBatch((ToolCall("call", "Read", {}),), "source")  # type: ignore[arg-type]


def test_conversation_rejects_duplicate_tool_protocol_ids() -> None:
    with pytest.raises(ValueError, match="duplicate tool call"):
        AssistantMessage(
            (
                ToolCall("call", "Read", {}),
                ToolCall("call", "Read", {}),
            ),
            TokenUsage(),
        )
    with pytest.raises(ValueError, match="duplicate result"):
        ToolResultBatch(
            (ToolResult("call", "first"), ToolResult("call", "second")),
            "assistant",
        )


def test_model_input_variants_reject_cross_semantic_content() -> None:
    with pytest.raises(TypeError, match="only input content"):
        UserInput((ModelToolUseBlock("call", "Read", {}),))  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="only text, tool calls"):
        AssistantOutput((InputText("bad"),))  # type: ignore[arg-type]

    user = UserInput((InputText("user"),))
    assistant = AssistantOutput((ModelTextBlock("assistant"),))
    outputs = ToolOutputs((ToolOutput("call", (ToolOutputText("done"),)),))
    assert (user.type, assistant.type, outputs.type) == (
        "user_input",
        "assistant_output",
        "tool_outputs",
    )


def test_model_input_tool_protocol_validation_is_role_free() -> None:
    call = AssistantOutput((ModelToolUseBlock("call", "Read", {}),))
    result = ToolOutputs((ToolOutput("call", (ToolOutputText("done"),)),))
    validate_model_input((call, result))

    with pytest.raises(ValueError, match="Orphan"):
        validate_model_input((result,))
    with pytest.raises(ValueError, match="Unresolved"):
        validate_model_input((call,))
    with pytest.raises(ValueError, match="Duplicate tool use"):
        validate_model_input(
            (
                AssistantOutput(
                    (
                        ModelToolUseBlock("call", "Read", {}),
                        ModelToolUseBlock("call", "Read", {}),
                    )
                ),
                result,
            )
        )
    with pytest.raises(ValueError, match="Duplicate tool output"):
        validate_model_input((call, result, result))
    with pytest.raises(ValueError, match="before next model input item"):
        validate_model_input((call, UserInput((InputText("injected"),)), result))
