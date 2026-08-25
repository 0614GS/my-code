import pytest

from my_code.context.microcompact import MicrocompactPolicy
from my_code.context.models import ContextOverflow
from my_code.context.planner import ContextPlanner
from my_code.context.session import ContextSnapshot as ConversationSnapshot
from my_code.context.window import ContextWindow
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
    PromptStability,
    ToolOutputs,
    ToolOutputText,
    UserInput,
)
from my_code.prompts.models import PromptSection
from my_code.prompts.registry import PromptRegistry


def _planner(max_chars: int = 1_000, microcompact=None) -> ContextPlanner:
    return ContextPlanner(
        window=ContextWindow(max_chars),
        prompt=PromptRegistry(
            (PromptSection("core", PromptStability.STATIC, lambda: "system"),)
        ),
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
    results = ToolResultBatch(
        (ToolResult("call", "value"),),
        assistant.uuid,
        parent_uuid=assistant.uuid,
    )
    summary = ConversationSummaryMessage("state", parent_uuid=results.uuid)

    messages = _planner().normalizer.normalize_transcript(
        (human, assistant, results, summary)
    )
    assert messages[0] == UserInput((InputText("hello"),))
    assert messages[1] == AssistantOutput(
        (
            ModelTextBlock("thinking"),
            ModelToolUseBlock("call", "Read", {"path": "x"}),
        )
    )
    assert isinstance(messages[2], ToolOutputs)
    result_content = messages[2].results[0].content[0]
    assert isinstance(result_content, ToolOutputText)
    assert result_content.text == "value"
    assert isinstance(messages[3], UserInput)
    assert "<conversation-summary>" in messages[3].content[0].text  # type: ignore[union-attr]


def test_projection_rejects_orphan_and_unresolved_tool_protocol() -> None:
    with pytest.raises(ValueError, match="Orphan"):
        _planner().normalizer.normalize_transcript(
            (ToolResultBatch((ToolResult("missing", "x"),), "assistant"),)
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
    results = ToolResultBatch(
        (ToolResult("call", "x" * 100),), assistant.uuid, parent_uuid=assistant.uuid
    )
    policy = MicrocompactPolicy(
        trigger_chars=50, target_chars=20, min_result_chars=10, keep_recent_results=0
    )
    plan = _planner(1_000, policy).plan(
        ConversationSnapshot((human, assistant, results)), tools=()
    )
    assert len(plan.new_content_replacements) == 1
    assert results.content[0].content == "x" * 100
    output = plan.request.input[-1]
    assert isinstance(output, ToolOutputs)
    result_content = output.results[0].content[0]
    assert isinstance(result_content, ToolOutputText)
    assert "compacted" in result_content.text
