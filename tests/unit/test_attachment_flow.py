from pathlib import Path

import pytest

from my_code.context.normalization import ModelInputNormalizer
from my_code.conversation.attachments import (
    FileMentionAttachment,
    InvokedSkillsAttachment,
    SkillActivationAttachment,
)
from my_code.conversation.models import (
    AssistantMessage,
    AttachmentMessage,
    ConversationSummaryMessage,
    HumanMessage,
    ToolCall,
    ToolResult,
    ToolResultBatch,
)
from my_code.conversation.state import CompactBoundary
from my_code.model.primitives import TokenUsage
from my_code.model.request import ToolOutputs, UserInput
from my_code.permissions.models import PermissionBehavior
from my_code.permissions.policy import PermissionPolicy
from my_code.sessions.session import Session
from my_code.skills.tool import restore_skill_permissions

SESSION_ID = "11111111-1111-1111-1111-111111111111"


def _tool_chain(session: Session) -> tuple[HumanMessage, AssistantMessage]:
    human = HumanMessage("use a skill")
    assistant = AssistantMessage(
        (ToolCall("skill", "Skill", {"skill": "focused"}),),
        TokenUsage(),
        parent_uuid=human.uuid,
    )
    session.append_human_message(human)
    session.append_assistant_message(assistant)
    return human, assistant


def test_tool_round_commits_results_before_durable_attachment(tmp_path: Path) -> None:
    session = Session(tmp_path, SESSION_ID)
    _, assistant = _tool_chain(session)
    activation = SkillActivationAttachment(
        "focused",
        "Follow focused instructions.",
        "project:workspace",
        ".my-code/skills/focused/SKILL.md",
        allowed_tools=("Read",),
    )
    batch = ToolResultBatch(
        (ToolResult("skill", "Launching skill: focused"),),
        assistant.uuid,
        parent_uuid=assistant.uuid,
    )

    _, attachments = session.commit_tool_round(batch, (activation,))

    assert [entry.kind for entry in session.snapshot().history] == [
        "human",
        "assistant",
        "tool_result_batch",
        "attachment",
    ]
    assert attachments[0].parent_uuid == batch.uuid
    assert (
        Session(tmp_path, SESSION_ID).snapshot().history == session.snapshot().history
    )
    model_input = ModelInputNormalizer().normalize((), session.snapshot().history)
    assert isinstance(model_input[-2], ToolOutputs)
    assert isinstance(model_input[-1], UserInput)


def test_tool_round_store_failure_leaves_memory_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = Session(tmp_path, SESSION_ID)
    _, assistant = _tool_chain(session)
    before = session.snapshot().history
    batch = ToolResultBatch(
        (ToolResult("skill", "ok"),), assistant.uuid, parent_uuid=assistant.uuid
    )

    def fail(_: object) -> None:
        raise OSError("write failed")

    monkeypatch.setattr(session._store, "_append_records", fail)  # type: ignore[attr-defined]
    with pytest.raises(OSError, match="write failed"):
        session.commit_tool_round(batch, (FileMentionAttachment("a.txt", "content"),))
    assert session.snapshot().history == before


def test_compact_rebuilds_latest_invoked_skills_and_restores_grants(
    tmp_path: Path,
) -> None:
    session = Session(tmp_path, SESSION_ID)
    human = HumanMessage("work")
    session.append_human_message(human)
    session.append_attachment(
        SkillActivationAttachment(
            "focused",
            "version one",
            "project:workspace",
            "focused/SKILL.md",
            allowed_tools=("Read",),
        )
    )
    latest = session.append_attachment(
        SkillActivationAttachment(
            "focused",
            "version two",
            "project:workspace",
            "focused/SKILL.md",
            allowed_tools=("Bash(git status)",),
        )
    )
    summary = ConversationSummaryMessage("state", parent_uuid=latest.uuid)
    boundary = CompactBoundary(latest.uuid, summary.uuid, "manual", 100)

    session.commit_compaction((), summary, boundary)

    invoked_message = session.snapshot().working_set[-1]
    assert isinstance(invoked_message, AttachmentMessage)
    assert isinstance(invoked_message.payload, InvokedSkillsAttachment)
    assert invoked_message.payload.skills[0].instructions == "version two"
    restored = Session(tmp_path, SESSION_ID)
    policy = PermissionPolicy()
    restore_skill_permissions(policy, restored.snapshot().history)
    assert any(
        rule.tool_name == "Bash"
        and rule.rule_content == "git status"
        and rule.behavior is PermissionBehavior.ALLOW
        for rule in policy.rules
    )
