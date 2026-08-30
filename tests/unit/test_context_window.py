import pytest

from my_code.context.microcompact import MicrocompactPolicy
from my_code.context.planner import ContextPlanner
from my_code.context.session import ContextPlanningState, ContextRuntime
from my_code.context.window import ContextWindow
from my_code.conversation.models import (
    AssistantMessage,
    ConversationEntry,
    ConversationSummaryMessage,
    HumanMessage,
    TextContent,
    ToolCall,
    ToolResult,
    ToolResultBatch,
)
from my_code.conversation.presentation import ToolResultPresentation
from my_code.model.primitives import (
    ProviderBinding,
    ProviderContinuationState,
    ProviderReplayRecord,
    TokenUsage,
    replay_content_id,
)
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
    long = HumanMessage("long")
    assert ContextWindow(2).ensure_fits((long,)) == (long,)


def test_four_conversation_variants_project_exactly() -> None:
    human = HumanMessage("hello")
    assistant = AssistantMessage(
        (TextContent("thinking"), ToolCall("call", "Read", {"path": "x"})),
        TokenUsage(input_tokens=3),
        parent_uuid=human.uuid,
    )
    results = ToolResultBatch(
        (ToolResult("call", "value", ToolResultPresentation("value")),),
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
            (
                ToolResultBatch(
                    (ToolResult("missing", "x", ToolResultPresentation("x")),),
                    "assistant",
                ),
            )
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
        (ToolResult("call", "x" * 100, ToolResultPresentation("result")),),
        assistant.uuid,
        parent_uuid=assistant.uuid,
    )
    policy = MicrocompactPolicy(
        trigger_chars=50, target_chars=20, min_result_chars=10, keep_recent_batches=0
    )
    plan = _planner(1_000, policy).plan(
        ContextPlanningState((human, assistant, results)), ContextRuntime(), tools=()
    )
    assert len(plan.new_content_replacements) == 1
    assert results.content[0].content == "x" * 100
    assert results.content[0].presentation == ToolResultPresentation("result")
    output = plan.request.input[-1]
    assert isinstance(output, ToolOutputs)
    result_content = output.results[0].content[0]
    assert isinstance(result_content, ToolOutputText)
    assert "removed from active context" in result_content.text
    assert "Run the tool again" not in result_content.text


def test_microcompact_trigger_includes_provider_replay_character_overhead() -> None:
    human = HumanMessage("read")
    assistant = AssistantMessage(
        (ToolCall("call", "Read", {"path": "x"}),),
        TokenUsage(),
        parent_uuid=human.uuid,
    )
    results = ToolResultBatch(
        (ToolResult("call", "x" * 100, ToolResultPresentation("result")),),
        assistant.uuid,
        parent_uuid=assistant.uuid,
    )
    policy = MicrocompactPolicy(
        trigger_chars=150,
        target_chars=100,
        min_result_chars=10,
        keep_recent_batches=0,
    )

    assert policy.propose((human, assistant, results), ()) == ()
    assert (
        len(policy.propose((human, assistant, results), (), additional_chars=100)) == 1
    )


def test_planner_microcompacts_before_replay_overflows_character_window() -> None:
    binding = ProviderBinding("anthropic-messages", "anthropic", "model")
    human = HumanMessage("read")
    assistant = AssistantMessage(
        (ToolCall("call", "Read", {"path": "x"}),),
        TokenUsage(),
        parent_uuid=human.uuid,
    )
    results = ToolResultBatch(
        (ToolResult("call", "x" * 100, ToolResultPresentation("result")),),
        assistant.uuid,
        parent_uuid=assistant.uuid,
    )
    replay = ProviderReplayRecord(
        assistant.uuid,
        replay_content_id(0),
        ProviderContinuationState(
            binding,
            "active_trajectory",
            {"type": "thinking", "signature": "s" * 180},
        ),
    )
    policy = MicrocompactPolicy(
        trigger_chars=150,
        target_chars=100,
        min_result_chars=10,
        keep_recent_batches=0,
    )
    planner = _planner(400, policy)
    planner.binding_resolver = lambda: binding

    plan = planner.plan(
        ContextPlanningState((human, assistant, results), replay_records=(replay,)),
        ContextRuntime(),
        tools=(),
    )

    assert len(plan.new_content_replacements) == 1


def test_microcompact_protects_complete_recent_parallel_batches() -> None:
    human = HumanMessage("read")
    entries: list[ConversationEntry] = [human]
    parent_uuid = human.uuid
    for index in range(3):
        assistant = AssistantMessage(
            (
                ToolCall(f"call-{index}-a", "Read", {"path": "a"}),
                ToolCall(f"call-{index}-b", "Grep", {"pattern": "x"}),
            ),
            TokenUsage(),
            parent_uuid=parent_uuid,
        )
        batch = ToolResultBatch(
            (
                ToolResult(f"call-{index}-a", "a" * 100, ToolResultPresentation("a")),
                ToolResult(f"call-{index}-b", "b" * 100, ToolResultPresentation("b")),
            ),
            assistant.uuid,
            parent_uuid=assistant.uuid,
        )
        entries.extend((assistant, batch))
        parent_uuid = batch.uuid
    policy = MicrocompactPolicy(
        trigger_chars=100,
        target_chars=1,
        min_result_chars=10,
        keep_recent_batches=2,
    )

    replacements = policy.propose(tuple(entries), ())

    assert {item.tool_use_id for item in replacements} == {
        "call-0-a",
        "call-0-b",
    }
