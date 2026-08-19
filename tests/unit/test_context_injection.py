import logging
from collections.abc import Iterable

import pytest

from nano_code.agent.errors import ContextOverflow
from nano_code.context import (
    AttachmentDelivery,
)
from nano_code.context import (
    ContextSnapshot as ConversationSnapshot,
)
from nano_code.context.attachments.models import (
    ContextAttachment,
    ContextObservation,
)
from nano_code.context.attachments.sources import DerivedAttachmentResolver
from nano_code.context.documents import ContextInstruction, UserContextDocument
from nano_code.context.normalization import ModelInputNormalizer
from nano_code.context.planner import ContextPlanner
from nano_code.context.window import ContextWindow
from nano_code.context.xml import render_context_instruction
from nano_code.conversation import (
    AssistantMessage,
    HumanMessage,
    ReasoningContent,
    TextContent,
    ToolCall,
    ToolResult,
    ToolResultsMessage,
)
from nano_code.model import (
    ModelReasoningBlock,
    ModelTextBlock,
    ModelUserMessage,
    PromptStability,
    ProviderBinding,
    ProviderContinuationState,
    ReasoningPresentation,
    TokenUsage,
)
from nano_code.prompts import (
    PromptRegistry,
    PromptSection,
)


def _planner(*, user_resolver=None, attachment_resolver=None) -> ContextPlanner:
    return ContextPlanner(
        window=ContextWindow(1_000),
        prompt=PromptRegistry(
            (PromptSection("core", PromptStability.STATIC, lambda: "system"),)
        ),
        tools=(),
        max_output_tokens=50,
        user_context_resolver=user_resolver,
        attachment_resolver=attachment_resolver,
    )


def test_request_context_shapes_are_strict() -> None:
    with pytest.raises(ValueError, match="source"):
        UserContextDocument(" ", (TextContent("context"),))
    with pytest.raises(ValueError, match="content"):
        ContextAttachment("hook", ())
    with pytest.raises(ValueError, match="empty"):
        ContextInstruction(" ")
    with pytest.raises(ValueError, match="retention"):
        ContextAttachment(
            "invalid",
            (TextContent("content"),),
            retention="forever",  # type: ignore[arg-type]
        )


def test_attachment_observation_projects_only_as_user_side_reminder() -> None:
    attachment = ContextAttachment(
        "file",
        (ContextObservation("File: notes.txt", "     1\thello"),),
    )

    result = ModelInputNormalizer().normalize(
        (), (HumanMessage("inspect"),), (attachment,)
    )

    assert len(result) == 1
    assert isinstance(result[0], ModelUserMessage)
    assert result[0].content[0] == ModelTextBlock("inspect")
    reminder = result[0].content[1]
    assert isinstance(reminder, ModelTextBlock)
    assert reminder.text.startswith("<system-reminder>")
    assert "explicitly attached" in reminder.text
    assert "notes.txt" in reminder.text


def test_attachment_projection_cannot_create_tool_protocol_blocks() -> None:
    attachment = ContextAttachment(
        "file", (ContextObservation("File: a.txt", "content"),)
    )
    projected = ModelInputNormalizer().attachment_projector.project(attachment)
    assert isinstance(projected, ModelUserMessage)
    assert all(isinstance(block, ModelTextBlock) for block in projected.content)


def test_opaque_thinking_replays_only_for_active_tool_trajectory() -> None:
    human = HumanMessage("inspect")
    opaque = ReasoningContent(
        "thinking",
        ReasoningPresentation("verbatim", ("hidden",)),
        ProviderContinuationState(
            ProviderBinding("anthropic-messages", "anthropic", "claude-test"),
            "active_trajectory",
            {"type": "thinking", "thinking": "hidden", "signature": "signed"},
        ),
    )
    assistant = AssistantMessage(
        (opaque, ToolCall("call", "Read", {"path": "x"})),
        TokenUsage(),
        parent_uuid=human.uuid,
    )
    results = ToolResultsMessage(
        (ToolResult("call", "value"),),
        assistant.uuid,
        parent_uuid=assistant.uuid,
    )
    normalizer = ModelInputNormalizer()

    active = normalizer.normalize((), (human, assistant, results), ())
    compact = normalizer.normalize_transcript((human, assistant, results))
    completed_assistant = AssistantMessage(
        (TextContent("done"),), TokenUsage(), parent_uuid=results.uuid
    )
    completed = normalizer.normalize(
        (), (human, assistant, results, completed_assistant), ()
    )

    assert any(
        isinstance(block, ModelReasoningBlock)
        for message in active
        for block in message.content
    )
    assert not any(
        isinstance(block, ModelReasoningBlock)
        for messages in (compact, completed)
        for message in messages
        for block in message.content
    )


def test_reminder_after_real_tool_result_stays_in_same_user_message() -> None:
    human = HumanMessage("inspect")
    assistant = AssistantMessage(
        (ToolCall("call", "Read", {"path": "x"}),),
        TokenUsage(),
        parent_uuid=human.uuid,
    )
    results = ToolResultsMessage(
        (ToolResult("call", "value"),),
        assistant.uuid,
        parent_uuid=assistant.uuid,
    )
    reminder = ContextAttachment("todo", (ContextInstruction("check todos"),))

    normalized = ModelInputNormalizer().normalize(
        (), (human, assistant, results), (reminder,)
    )

    last = normalized[-1]
    assert isinstance(last, ModelUserMessage)
    assert last.content[0].type == "tool_result"
    assert last.content[1].type == "text"
    assert "<system-reminder>" in last.content[1].text  # type: ignore[union-attr]


def test_normalizer_orders_context_history_and_attachments_then_merges() -> None:
    instruction = ContextInstruction("keep active")
    result = ModelInputNormalizer().normalize(
        (UserContextDocument("AGENTS.md", (instruction,)),),
        (HumanMessage("prompt"),),
        (ContextAttachment("hook", (TextContent("attachment"),)),),
    )

    assert result == (
        ModelUserMessage(
            (
                ModelTextBlock(render_context_instruction(instruction)),
                ModelTextBlock("prompt"),
                ModelTextBlock("attachment"),
            )
        ),
    )


def test_attachment_resolver_runs_sources_in_order() -> None:
    snapshot = ConversationSnapshot(())
    first = ContextAttachment("first", (TextContent("one"),))
    second = ContextAttachment("second", (TextContent("two"),))

    def first_source(_: ConversationSnapshot) -> Iterable[ContextAttachment]:
        return (first,)

    def second_source(_: ConversationSnapshot) -> Iterable[ContextAttachment]:
        return (second,)

    assert DerivedAttachmentResolver((first_source, second_source)).resolve(
        snapshot
    ) == (
        first,
        second,
    )


def test_attachment_resolver_skips_failed_source_atomically(
    caplog: pytest.LogCaptureFixture,
) -> None:
    partial = ContextAttachment("partial", (TextContent("discarded"),))
    healthy = ContextAttachment("healthy", (TextContent("kept"),))

    def broken(_: ConversationSnapshot) -> Iterable[ContextAttachment]:
        yield partial
        raise RuntimeError("failed")

    def good(_: ConversationSnapshot) -> Iterable[ContextAttachment]:
        return (healthy,)

    with caplog.at_level(logging.ERROR, logger="nano_code.context.attachments"):
        result = DerivedAttachmentResolver((broken, good)).resolve(
            ConversationSnapshot(())
        )
    assert result == (healthy,)


def test_planner_caches_user_context_and_excludes_attachments_from_compact() -> None:
    document = UserContextDocument("memory", (TextContent("stable"),))

    class Resolver:
        calls = 0

        def resolve(self) -> tuple[UserContextDocument, ...]:
            self.calls += 1
            return (document,)

    resolver = Resolver()
    counter = 0

    def attachment(_: ConversationSnapshot) -> tuple[ContextAttachment, ...]:
        nonlocal counter
        counter += 1
        return (ContextAttachment("dynamic", (TextContent(str(counter)),)),)

    planner = _planner(
        user_resolver=resolver,
        attachment_resolver=DerivedAttachmentResolver((attachment,)),
    )
    snapshot = ConversationSnapshot((HumanMessage("prompt"),))
    first = planner.plan(snapshot)
    second = planner.plan(snapshot)
    compact, replacements = planner.compaction_view(snapshot)

    assert resolver.calls == 1
    assert first.request.messages != second.request.messages
    assert compact == (ModelUserMessage((ModelTextBlock("prompt"),)),)
    assert replacements == ()


def test_budget_separates_request_and_delivered_attachment_chars() -> None:
    human = HumanMessage("prompt")
    delivered = ContextAttachment(
        "event", (TextContent("old"),), retention="live_session"
    )
    current = ContextAttachment("derived", (TextContent("new"),))
    planner = _planner(
        attachment_resolver=DerivedAttachmentResolver((lambda _: (current,),))
    )

    plan = planner.plan(
        ConversationSnapshot(
            (human,),
            attachment_deliveries=(AttachmentDelivery(human.uuid, delivered),),
        )
    )

    assert plan.budget is not None
    assert plan.budget.message_chars == len("prompt")
    assert plan.budget.attachment_chars == len("oldnew")


def test_attachment_chars_participate_in_context_window() -> None:
    attachment = ContextAttachment("large", (TextContent("x" * 20),))
    planner = ContextPlanner(
        window=ContextWindow(10),
        prompt=PromptRegistry(
            (PromptSection("core", PromptStability.STATIC, lambda: "system"),)
        ),
        tools=(),
        max_output_tokens=10,
        attachment_resolver=DerivedAttachmentResolver((lambda _: (attachment,),)),
    )

    with pytest.raises(ContextOverflow):
        planner.plan(ConversationSnapshot((HumanMessage("p"),)))
