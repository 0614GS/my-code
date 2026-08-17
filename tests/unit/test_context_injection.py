import logging
from collections.abc import Iterable

import pytest

from nano_code.agent import (
    AttachmentDelivery,
    ConversationSnapshot,
    ModelAssistantMessage,
    ModelTextBlock,
    ModelToolResultBlock,
    ModelToolUseBlock,
    ModelUserMessage,
)
from nano_code.agent.errors import ContextOverflow
from nano_code.context import (
    ContextPlanner,
    ContextWindow,
    DerivedAttachmentResolver,
    ModelInputNormalizer,
)
from nano_code.messages import (
    AttachmentToolExchange,
    ContextAttachment,
    ContextInstruction,
    HumanMessage,
    TextContent,
    UserContextDocument,
)
from nano_code.messages.xml import render_context_instruction
from nano_code.prompts import PromptRegistry, PromptSection, PromptStability


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


def test_attachment_tool_exchange_projects_as_closed_protocol_pair() -> None:
    attachment = ContextAttachment(
        "file",
        (
            AttachmentToolExchange(
                "Read",
                {"path": "notes.txt"},
                "1→hello",
                tool_use_id="attachment-read",
            ),
        ),
    )

    result = ModelInputNormalizer().normalize(
        (), (HumanMessage("inspect"),), (attachment,)
    )

    assert result == (
        ModelUserMessage((ModelTextBlock("inspect"),)),
        ModelAssistantMessage(
            (ModelToolUseBlock("attachment-read", "Read", {"path": "notes.txt"}),)
        ),
        ModelUserMessage((ModelToolResultBlock("attachment-read", "1→hello", False),)),
    )


def test_attachment_tool_ids_share_global_protocol_validation() -> None:
    attachments = tuple(
        ContextAttachment(
            source,
            (
                AttachmentToolExchange(
                    "Read",
                    {"path": f"{source}.txt"},
                    source,
                    tool_use_id="duplicate",
                ),
            ),
        )
        for source in ("first", "second")
    )

    with pytest.raises(ValueError, match="Duplicate tool use"):
        ModelInputNormalizer().normalize((), (HumanMessage("inspect"),), attachments)


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
