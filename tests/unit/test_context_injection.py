import logging
from collections.abc import Iterable

import pytest

from nano_code.agent import (
    ConversationSnapshot,
    ModelTextBlock,
    ModelUserMessage,
)
from nano_code.context import (
    AttachmentResolver,
    ContextPlanner,
    ContextWindow,
    ModelInputNormalizer,
)
from nano_code.messages import (
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

    assert AttachmentResolver((first_source, second_source)).resolve(snapshot) == (
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
        result = AttachmentResolver((broken, good)).resolve(ConversationSnapshot(()))
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
        attachment_resolver=AttachmentResolver((attachment,)),
    )
    snapshot = ConversationSnapshot((HumanMessage("prompt"),))
    first = planner.plan(snapshot)
    second = planner.plan(snapshot)
    compact, replacements = planner.compaction_view(snapshot)

    assert resolver.calls == 1
    assert first.request.messages != second.request.messages
    assert compact == (ModelUserMessage((ModelTextBlock("prompt"),)),)
    assert replacements == ()
