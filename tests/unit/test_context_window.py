import pytest

from nano_code.context.microcompact import MicrocompactPolicy
from nano_code.context.models import ContextOverflow
from nano_code.context.planner import ContextBuilder
from nano_code.context.session import ContextSnapshot as ConversationSnapshot
from nano_code.context.window import ContextWindow
from nano_code.conversation.models import (
    AssistantMessage,
    ConversationSummaryMessage,
    HumanMessage,
    TextContent,
    ToolCall,
    ToolResult,
    ToolResultsMessage,
)
from nano_code.model.primitives import TokenUsage
from nano_code.model.request import (
    ModelAssistantMessage,
    ModelTextBlock,
    ModelToolResultBlock,
    ModelToolUseBlock,
    ModelUserMessage,
    PromptStability,
)
from nano_code.prompts.models import PromptSection
from nano_code.prompts.registry import PromptRegistry


def _planner(max_chars: int = 1_000, microcompact=None) -> ContextBuilder:
    return ContextBuilder(
        window=ContextWindow(max_chars),
        prompt=PromptRegistry(
            (PromptSection("core", PromptStability.STATIC, lambda: "system"),)
        ),
        tools=(),
        max_output_tokens=50,
        microcompact=microcompact,
    )


def test_context_window_requires_semantic_boundary_and_never_truncates() -> None:
    assistant = AssistantMessage((TextContent("answer"),), TokenUsage())
    with pytest.raises(ValueError, match="boundary"):
        ContextWindow().ensure_fits((assistant,))
    with pytest.raises(ContextOverflow):
        ContextWindow(2).ensure_fits((HumanMessage("long"),))


def test_four_conversation_variants_project_exactly() -> None:
    human = HumanMessage("hello")
    assistant = AssistantMessage(
        (TextContent("thinking"), ToolCall("call", "Read", {"path": "x"})),
        TokenUsage(input_tokens=3),
        parent_uuid=human.uuid,
    )
    results = ToolResultsMessage(
        (ToolResult("call", "value"),),
        assistant.uuid,
        parent_uuid=assistant.uuid,
    )
    summary = ConversationSummaryMessage("state", parent_uuid=results.uuid)

    messages = _planner().normalizer.normalize_transcript(
        (human, assistant, results, summary)
    )
    assert messages[0] == ModelUserMessage((ModelTextBlock("hello"),))
    assert messages[1] == ModelAssistantMessage(
        (
            ModelTextBlock("thinking"),
            ModelToolUseBlock("call", "Read", {"path": "x"}),
        )
    )
    assert messages[2].content[0] == ModelToolResultBlock("call", "value")
    assert "<conversation-summary>" in messages[2].content[1].text  # type: ignore[union-attr]


def test_projection_rejects_orphan_and_unresolved_tool_protocol() -> None:
    with pytest.raises(ValueError, match="Orphan"):
        _planner().normalizer.normalize_transcript(
            (ToolResultsMessage((ToolResult("missing", "x"),), "assistant"),)
        )
    with pytest.raises(ValueError, match="Unresolved"):
        _planner().normalizer.normalize_transcript(
            (AssistantMessage((ToolCall("call", "Read", {}),), TokenUsage()),)
        )


def test_microcompact_replaces_model_view_without_mutating_history() -> None:
    human = HumanMessage("read")
    assistant = AssistantMessage(
        (ToolCall("call", "Read", {"path": "x"}),),
        TokenUsage(),
        parent_uuid=human.uuid,
    )
    results = ToolResultsMessage(
        (ToolResult("call", "x" * 100),), assistant.uuid, parent_uuid=assistant.uuid
    )
    policy = MicrocompactPolicy(
        trigger_chars=50, target_chars=20, min_result_chars=10, keep_recent_results=0
    )
    plan = _planner(1_000, policy).plan(
        ConversationSnapshot((human, assistant, results))
    )
    assert len(plan.new_content_replacements) == 1
    assert results.content[0].content == "x" * 100
    assert "compacted" in plan.request.messages[-1].content[0].content  # type: ignore[union-attr]
