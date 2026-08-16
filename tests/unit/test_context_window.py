from pathlib import Path

import pytest

from nano_code.agent import (
    ContextOverflow,
    ConversationSnapshot,
    EphemeralContextMessage,
    ModelInputMessage,
    ToolDefinition,
)
from nano_code.context import (
    ContextPlanner,
    ContextWindow,
    MicrocompactPolicy,
    ModelInputNormalizer,
)
from nano_code.messages import (
    SystemContextBlock,
    TextBlock,
    TokenUsage,
    ToolResultBlock,
    ToolUseBlock,
    TranscriptMessage,
)
from nano_code.messages.xml import render_system_context
from nano_code.prompts import PromptRegistry, PromptSection, PromptStability


def user(text: str, parent: str | None = None) -> TranscriptMessage:
    return TranscriptMessage(
        role="user",
        origin="human",
        content=(TextBlock(text),),
        parent_uuid=parent,
    )


def test_window_reports_overflow_instead_of_silently_dropping_history() -> None:
    first = user("old prompt")
    first_answer = TranscriptMessage(
        role="assistant",
        origin="model",
        content=(TextBlock("old answer"),),
        parent_uuid=first.uuid,
    )
    current = user("new", first_answer.uuid)
    tool_use = TranscriptMessage(
        role="assistant",
        origin="model",
        content=(ToolUseBlock("call", "Read", {"path": "x"}),),
        parent_uuid=current.uuid,
    )
    tool_result = TranscriptMessage(
        role="user",
        origin="tool",
        content=(ToolResultBlock("call", "value"),),
        parent_uuid=tool_use.uuid,
    )

    with pytest.raises(ContextOverflow) as raised:
        ContextWindow(max_chars=40).ensure_fits(
            (first, first_answer, current, tool_use, tool_result)
        )

    assert raised.value.current_chars > raised.value.max_chars


def test_normalization_rejects_orphan_tool_result() -> None:
    prompt = user("new")
    result = TranscriptMessage(
        role="user",
        origin="tool",
        content=(ToolResultBlock("missing", "value"),),
        parent_uuid=prompt.uuid,
    )

    selected = ContextWindow().ensure_fits((prompt, result))

    with pytest.raises(ValueError, match="Orphan tool result"):
        ModelInputNormalizer().normalize_transcript(selected)


def test_context_planner_builds_observable_request_without_mutating_snapshot() -> None:
    message = user("hello")
    snapshot = ConversationSnapshot((message,))
    section = PromptSection("core", PromptStability.STATIC, lambda: "system")
    tool = ToolDefinition("Read", "Read a file", {"type": "object"})
    planner = ContextPlanner(
        window=ContextWindow(max_chars=100),
        prompt=PromptRegistry((section,)),
        tools=(tool,),
        max_output_tokens=50,
    )

    plan = planner.plan(snapshot)

    assert plan.system_prompt.text == "system"
    assert plan.messages == (ModelInputMessage("user", (TextBlock("hello"),)),)
    assert plan.tools == (tool,)
    assert plan.system_prompt.sections[0].key == section.key
    assert plan.budget is not None
    assert plan.budget.message_chars == len("hello")
    assert plan.budget.system_chars == len("system")
    assert plan.budget.last_actual_input_tokens is None
    assert snapshot.messages == (message,)


def test_workspace_context_is_cached_budgeted_and_excluded_from_compaction() -> None:
    message = user("hello")
    workspace_message = EphemeralContextMessage(
        role="user",
        content=(
            SystemContextBlock(kind="system_reminder", content="workspace facts"),
        ),
    )
    normalized_workspace = ModelInputMessage(
        "user",
        (TextBlock(render_system_context(workspace_message.content[0])),),
    )

    class Resolver:
        calls = 0

        def resolve(self) -> tuple[EphemeralContextMessage, ...]:
            self.calls += 1
            return (workspace_message,)

    resolver = Resolver()
    planner = ContextPlanner(
        window=ContextWindow(max_chars=100),
        prompt=PromptRegistry(
            (PromptSection("core", PromptStability.STATIC, lambda: "system"),)
        ),
        tools=(),
        max_output_tokens=50,
        workspace_context_resolver=resolver,
    )

    plan = planner.plan(ConversationSnapshot((message,)))
    budget = planner.inspect(ConversationSnapshot((message,)))
    compact_messages, replacements = planner.compaction_view(
        ConversationSnapshot((message,))
    )

    assert resolver.calls == 1
    assert plan.workspace_context == (normalized_workspace,)
    assert budget.workspace_context_chars == len(
        normalized_workspace.content[0].text
    )
    assert budget.estimated_input_chars == (
        len("hello") + len("system") + len(normalized_workspace.content[0].text)
    )
    assert compact_messages == (ModelInputMessage("user", (TextBlock("hello"),)),)
    assert replacements == ()


def test_planner_without_workspace_resolver_does_not_read_agents_file(
    tmp_path: Path,
) -> None:
    (tmp_path / "AGENTS.md").write_text("unrequested instructions", encoding="utf-8")
    planner = ContextPlanner(
        window=ContextWindow(max_chars=100),
        prompt=PromptRegistry(
            (PromptSection("core", PromptStability.STATIC, lambda: "system"),)
        ),
        tools=(),
        max_output_tokens=50,
    )

    plan = planner.plan(ConversationSnapshot((user("hello"),)))

    assert plan.workspace_context == ()


def test_prompt_registry_rejects_duplicate_section_keys() -> None:
    sections = (
        PromptSection("core", PromptStability.STATIC, lambda: "first"),
        PromptSection("core", PromptStability.SESSION, lambda: "second"),
    )

    with pytest.raises(ValueError, match="must be unique"):
        PromptRegistry(sections)


def test_model_normalization_merges_adjacent_roles_without_local_metadata() -> None:
    first = user("first")
    second = user("second", first.uuid)

    normalized = ModelInputNormalizer().normalize_transcript((first, second))

    assert normalized == (
        ModelInputMessage("user", (TextBlock("first"), TextBlock("second"))),
    )


def test_budget_uses_latest_real_usage_plus_only_new_messages() -> None:
    first = user("old prompt")
    assistant = TranscriptMessage(
        role="assistant",
        origin="model",
        content=(TextBlock("answer"),),
        parent_uuid=first.uuid,
        usage=TokenUsage(input_tokens=100, output_tokens=10),
    )
    latest = user("next", assistant.uuid)
    planner = ContextPlanner(
        window=ContextWindow(max_chars=1000),
        prompt=PromptRegistry(
            (PromptSection("core", PromptStability.STATIC, lambda: "system"),)
        ),
        tools=(),
        max_output_tokens=50,
    )

    budget = planner.plan(ConversationSnapshot((first, assistant, latest))).budget

    assert budget is not None
    assert budget.last_actual_input_tokens == 100
    assert budget.incremental_tokens == 1
    assert budget.estimated_input_tokens == 111
    assert budget.estimated_total_tokens == 161


def test_microcompact_replaces_old_result_without_mutating_transcript() -> None:
    prompt = user("inspect")
    first_call = TranscriptMessage(
        role="assistant",
        origin="model",
        content=(ToolUseBlock("old", "Read", {"path": "old.py"}),),
        parent_uuid=prompt.uuid,
    )
    first_result = TranscriptMessage(
        role="user",
        origin="tool",
        content=(ToolResultBlock("old", "x" * 80),),
        parent_uuid=first_call.uuid,
    )
    second_call = TranscriptMessage(
        role="assistant",
        origin="model",
        content=(ToolUseBlock("new", "Read", {"path": "new.py"}),),
        parent_uuid=first_result.uuid,
    )
    second_result = TranscriptMessage(
        role="user",
        origin="tool",
        content=(ToolResultBlock("new", "y" * 80),),
        parent_uuid=second_call.uuid,
    )
    planner = ContextPlanner(
        window=ContextWindow(max_chars=1000),
        prompt=PromptRegistry(
            (PromptSection("core", PromptStability.STATIC, lambda: "system"),)
        ),
        tools=(),
        max_output_tokens=50,
        microcompact=MicrocompactPolicy(
            trigger_chars=100,
            target_chars=90,
            min_result_chars=1,
            keep_recent_results=1,
        ),
    )
    snapshot = ConversationSnapshot(
        (prompt, first_call, first_result, second_call, second_result)
    )

    plan = planner.plan(snapshot)

    assert [item.tool_use_id for item in plan.new_content_replacements] == ["old"]
    normalized_old = plan.messages[2].content[0]
    assert isinstance(normalized_old, ToolResultBlock)
    assert "compacted" in normalized_old.content
    original_old = first_result.content[0]
    assert isinstance(original_old, ToolResultBlock)
    assert original_old.content == "x" * 80

    replayed = planner.plan(
        ConversationSnapshot(
            snapshot.messages,
            content_replacements=plan.new_content_replacements,
        )
    )
    assert replayed.messages == plan.messages
    assert replayed.new_content_replacements == ()
