import logging
from collections.abc import Iterable

import pytest

from my_code.context.attachments.sources import DerivedAttachmentResolver
from my_code.context.documents import ContextInstruction, UserContextDocument
from my_code.context.models import ContextOverflow
from my_code.context.normalization import ModelInputNormalizer
from my_code.context.planner import ContextPlanner
from my_code.context.session import (
    AttachmentDerivationState,
    ContextPlanningState,
    ContextRuntime,
)
from my_code.context.window import ContextWindow
from my_code.context.xml import render_context_instruction
from my_code.conversation.attachments import (
    FileMentionAttachment,
    TodoReminderAttachment,
)
from my_code.conversation.models import (
    AssistantMessage,
    AttachmentMessage,
    HumanMessage,
    ToolCall,
    ToolResult,
    ToolResultBatch,
)
from my_code.conversation.presentation import ToolResultPresentation
from my_code.model.primitives import TokenUsage
from my_code.model.request import InputText, PromptStability, ToolOutputs, UserInput
from my_code.prompts.models import PromptSection
from my_code.prompts.registry import PromptRegistry


def _planner(*, attachment_resolver=None) -> ContextPlanner:
    return ContextPlanner(
        window=ContextWindow(1_000),
        prompt=PromptRegistry(
            (PromptSection("core", PromptStability.STATIC, lambda: "system"),)
        ),
        max_output_tokens=50,
        attachment_resolver=attachment_resolver,
    )


def test_attachment_projects_only_as_user_input() -> None:
    attachment = AttachmentMessage(
        FileMentionAttachment("notes.txt", "     1\thello"), parent_uuid="human"
    )
    result = ModelInputNormalizer().normalize(
        (), (HumanMessage("inspect", uuid="human"), attachment)
    )
    assert isinstance(result[-1], UserInput)
    assert all(isinstance(block, InputText) for block in result[-1].content)
    block = result[-1].content[0]
    assert isinstance(block, InputText)
    assert "explicitly attached" in block.text


def test_attachment_keeps_original_position_after_tool_results() -> None:
    human = HumanMessage("inspect")
    assistant = AssistantMessage(
        (ToolCall("call", "Read", {"path": "x"}),),
        TokenUsage(),
        parent_uuid=human.uuid,
    )
    results = ToolResultBatch(
        (ToolResult("call", "value", ToolResultPresentation("value")),),
        assistant.uuid,
        parent_uuid=assistant.uuid,
    )
    reminder = AttachmentMessage(
        TodoReminderAttachment("check todos"), parent_uuid=results.uuid
    )
    normalized = ModelInputNormalizer().normalize(
        (), (human, assistant, results, reminder)
    )
    assert isinstance(normalized[-2], ToolOutputs)
    assert isinstance(normalized[-1], UserInput)
    block = normalized[-1].content[0]
    assert isinstance(block, InputText)
    assert "<system-reminder>" in block.text


def test_normalizer_orders_user_context_and_conversation() -> None:
    instruction = ContextInstruction("keep active")
    human = HumanMessage("prompt")
    attachment = AttachmentMessage(
        FileMentionAttachment("a.txt", "attachment"), parent_uuid=human.uuid
    )
    result = ModelInputNormalizer().normalize(
        (UserContextDocument("AGENTS.md", (instruction,)),), (human, attachment)
    )
    assert result[0] == UserInput((InputText(render_context_instruction(instruction)),))
    assert result[1] == UserInput((InputText("prompt"),))
    assert isinstance(result[2], UserInput)


def test_attachment_resolver_runs_sources_in_order() -> None:
    first = TodoReminderAttachment("one")
    second = TodoReminderAttachment("two")

    def first_source(_: AttachmentDerivationState) -> Iterable[TodoReminderAttachment]:
        return (first,)

    def second_source(_: AttachmentDerivationState) -> Iterable[TodoReminderAttachment]:
        return (second,)

    assert DerivedAttachmentResolver((first_source, second_source)).resolve(
        AttachmentDerivationState("session", (), ())
    ) == (first, second)


def test_attachment_resolver_discards_partial_failed_source(
    caplog: pytest.LogCaptureFixture,
) -> None:
    partial = TodoReminderAttachment("discarded")
    healthy = TodoReminderAttachment("kept")

    def broken(_: AttachmentDerivationState) -> Iterable[TodoReminderAttachment]:
        yield partial
        raise RuntimeError("failed")

    def good(_: AttachmentDerivationState) -> Iterable[TodoReminderAttachment]:
        return (healthy,)

    with caplog.at_level(logging.ERROR):
        result = DerivedAttachmentResolver((broken, good)).resolve(
            AttachmentDerivationState("session", (), ())
        )
    assert result == (healthy,)


def test_budget_reports_attachment_chars_separately() -> None:
    human = HumanMessage("prompt")
    attachment = AttachmentMessage(
        FileMentionAttachment("a.txt", "old"), parent_uuid=human.uuid
    )
    plan = _planner().plan(
        ContextPlanningState((human, attachment)), ContextRuntime(), tools=()
    )
    assert plan.budget is not None
    assert plan.budget.message_chars == len("prompt")
    assert plan.budget.attachment_chars > len("old")


def test_attachment_chars_participate_in_context_window() -> None:
    human = HumanMessage("p")
    attachment = AttachmentMessage(
        FileMentionAttachment("a.txt", "x" * 30), parent_uuid=human.uuid
    )
    planner = ContextPlanner(
        window=ContextWindow(10),
        prompt=PromptRegistry(
            (PromptSection("core", PromptStability.STATIC, lambda: "system"),)
        ),
        max_output_tokens=10,
    )
    with pytest.raises(ContextOverflow):
        planner.plan(
            ContextPlanningState((human, attachment)), ContextRuntime(), tools=()
        )
