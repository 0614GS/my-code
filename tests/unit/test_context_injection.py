import logging
from collections.abc import Iterable
from pathlib import Path

import pytest

from nano_code.agent import ConversationSnapshot, ModelInputMessage
from nano_code.context import (
    AttachmentResolver,
    ContextPlanner,
    ContextWindow,
    ModelInputNormalizer,
    UserContextResolver,
)
from nano_code.messages import (
    AttachmentMessage,
    SystemContextBlock,
    TextBlock,
    TranscriptMessage,
    UserContextMessage,
)
from nano_code.messages.xml import render_system_context
from nano_code.prompts import (
    PromptRegistry,
    PromptSection,
    PromptStability,
    build_system_prompt_registry,
)


def _planner(
    *,
    user_context_resolver: UserContextResolver | None = None,
    attachment_resolver: AttachmentResolver | None = None,
) -> ContextPlanner:
    return ContextPlanner(
        window=ContextWindow(max_chars=1_000),
        prompt=PromptRegistry(
            (PromptSection("core", PromptStability.STATIC, lambda: "system"),)
        ),
        tools=(),
        max_output_tokens=50,
        user_context_resolver=user_context_resolver,
        attachment_resolver=attachment_resolver,
    )


def test_context_message_shapes_require_source_and_content() -> None:
    with pytest.raises(ValueError, match="source"):
        UserContextMessage(" ", (TextBlock("context"),))
    with pytest.raises(ValueError, match="contain"):
        UserContextMessage("AGENTS.md", ())
    with pytest.raises(ValueError, match="source"):
        AttachmentMessage("", (TextBlock("attachment"),))
    with pytest.raises(ValueError, match="contain"):
        AttachmentMessage("hook", ())


def test_normalizer_projects_each_non_history_collection_and_renders_xml() -> None:
    reminder = SystemContextBlock(
        kind="system_reminder", content="Keep the reminder active."
    )
    user_context = (
        UserContextMessage("memory", (TextBlock("first"),)),
        UserContextMessage("AGENTS.md", (reminder,)),
    )
    attachment = AttachmentMessage("hook", (reminder,))
    normalizer = ModelInputNormalizer()

    normalized_user = normalizer.normalize_user_context(user_context)
    normalized_attachment = normalizer.normalize_attachments((attachment,))

    assert normalized_user == (
        ModelInputMessage(
            "user",
            (TextBlock("first"), TextBlock(render_system_context(reminder))),
        ),
    )
    assert normalized_attachment == (
        ModelInputMessage("user", (TextBlock(render_system_context(reminder)),)),
    )


def test_attachment_resolver_defaults_to_empty_tuple() -> None:
    snapshot = ConversationSnapshot(())

    assert AttachmentResolver().resolve(snapshot) == ()


def test_attachment_resolver_runs_sources_in_order_for_each_snapshot() -> None:
    snapshot = ConversationSnapshot(())
    first = AttachmentMessage("first", (TextBlock("one"),))
    second = AttachmentMessage("second", (TextBlock("two"),))
    calls: list[tuple[str, ConversationSnapshot]] = []

    def first_source(current: ConversationSnapshot) -> Iterable[AttachmentMessage]:
        calls.append(("first", current))
        return (first,)

    def second_source(current: ConversationSnapshot) -> Iterable[AttachmentMessage]:
        calls.append(("second", current))
        return (second,)

    resolver = AttachmentResolver((first_source, second_source))

    assert resolver.resolve(snapshot) == (first, second)
    assert resolver.resolve(snapshot) == (first, second)
    assert calls == [
        ("first", snapshot),
        ("second", snapshot),
        ("first", snapshot),
        ("second", snapshot),
    ]


def test_attachment_resolver_skips_failed_source_atomically(
    caplog: pytest.LogCaptureFixture,
) -> None:
    snapshot = ConversationSnapshot(())
    partial = AttachmentMessage("partial", (TextBlock("discarded"),))
    healthy = AttachmentMessage("healthy", (TextBlock("kept"),))

    def broken_source(_: ConversationSnapshot) -> Iterable[AttachmentMessage]:
        yield partial
        raise RuntimeError("source failed")

    def healthy_source(_: ConversationSnapshot) -> Iterable[AttachmentMessage]:
        return (healthy,)

    resolver = AttachmentResolver((broken_source, healthy_source))
    with caplog.at_level(logging.ERROR, logger="nano_code.context.attachments"):
        resolved = resolver.resolve(snapshot)

    assert resolved == (healthy,)
    assert "Attachment source failed" in caplog.text


def test_environment_context_is_a_system_prompt_section_only(tmp_path: Path) -> None:
    planner = ContextPlanner(
        window=ContextWindow(max_chars=1_000),
        prompt=build_system_prompt_registry(tmp_path),
        tools=(),
        max_output_tokens=50,
    )

    plan = planner.plan(
        ConversationSnapshot(
            (
                TranscriptMessage(
                    role="user", origin="human", content=(TextBlock("prompt"),)
                ),
            )
        )
    )

    assert str(tmp_path.resolve()) in plan.system_prompt.text
    assert plan.user_context == ()
    assert plan.attachments == ()


def test_planner_caches_user_context_but_resolves_attachments_per_request() -> None:
    user_context = UserContextMessage(
        "AGENTS.md",
        (SystemContextBlock(kind="system_reminder", content="instructions"),),
    )

    class UserResolver:
        calls = 0

        def resolve(self) -> tuple[UserContextMessage, ...]:
            self.calls += 1
            return (user_context,)

    user_resolver = UserResolver()
    attachment_snapshots: list[ConversationSnapshot] = []

    def attachment_source(
        current_snapshot: ConversationSnapshot,
    ) -> tuple[AttachmentMessage, ...]:
        attachment_snapshots.append(current_snapshot)
        return (
            AttachmentMessage(
                "dynamic", (TextBlock(str(len(attachment_snapshots))),)
            ),
        )

    attachment_resolver = AttachmentResolver((attachment_source,))
    planner = _planner(
        user_context_resolver=user_resolver,
        attachment_resolver=attachment_resolver,
    )
    snapshot = ConversationSnapshot(
        (
            TranscriptMessage(
                role="user", origin="human", content=(TextBlock("prompt"),)
            ),
        )
    )

    first = planner.plan(snapshot)
    second = planner.plan(snapshot)
    planner.inspect(snapshot)
    compact_messages, replacements = planner.compaction_view(snapshot)

    assert user_resolver.calls == 1
    assert attachment_snapshots == [snapshot, snapshot, snapshot]
    assert all(current is snapshot for current in attachment_snapshots)
    assert first.user_context == second.user_context
    assert first.attachments != second.attachments
    assert compact_messages == (
        ModelInputMessage("user", (TextBlock("prompt"),)),
    )
    assert replacements == ()
    assert first.budget is not None
    assert first.user_context
    user_block = first.user_context[0].content[0]
    assert isinstance(user_block, TextBlock)
    assert first.budget.user_context_chars == len(user_block.text)
    assert first.budget.attachment_chars == 1
    assert first.budget.estimated_input_chars == (
        first.budget.system_chars
        + first.budget.user_context_chars
        + first.budget.message_chars
        + first.budget.attachment_chars
        + first.budget.tool_schema_chars
    )
