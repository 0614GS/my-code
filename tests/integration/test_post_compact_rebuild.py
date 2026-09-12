import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from my_code.context.compaction import ContextCompactor
from my_code.context.engine import ContextEngine
from my_code.context.planner import ContextPlanner
from my_code.context.rebuild import PostCompactContextRebuilder
from my_code.context.session_cache import CompactionInput, SessionContextCache
from my_code.conversation.attachments import (
    AttachmentPayload,
    CollaborationModeAttachment,
    FileMentionAttachment,
    InvokedSkillsAttachment,
    SkillActivationAttachment,
    TodoSnapshotAttachment,
    ToolDiscoveryAttachment,
    ToolDiscoveryDefinition,
    ToolDiscoveryInvalidationAttachment,
)
from my_code.conversation.models import (
    AssistantMessage,
    AttachmentMessage,
    HumanMessage,
    TextContent,
    ToolCall,
    ToolResult,
)
from my_code.conversation.presentation import ToolResultPresentation
from my_code.conversation.state import CompactTrigger
from my_code.features.todos.projection import project_todos
from my_code.features.todos.rebuild import TodoPostCompactAttachmentSource
from my_code.features.todos.reminder import TodoReminderAttachmentSource
from my_code.foundation.json import JsonObject
from my_code.model.events import ModelOutputCompleted, ModelStreamEvent
from my_code.model.primitives import TokenUsage
from my_code.model.request import (
    ModelOutput,
    ModelRequest,
    ModelTextBlock,
    PromptStability,
)
from my_code.prompts.models import PromptSection
from my_code.prompts.registry import PromptRegistry
from my_code.sessions.session import Session

SESSION_ID = "11111111-1111-1111-1111-111111111111"


class SummaryModel:
    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
        yield ModelStreamEvent(
            0,
            ModelOutputCompleted(
                ModelOutput(
                    (ModelTextBlock("Continue work."),),
                    "end_turn",
                    TokenUsage(10, 4, provider_reported=True),
                )
            ),
        )


def engine(rebuilder: PostCompactContextRebuilder | None = None) -> ContextEngine:
    return ContextEngine(
        ContextPlanner(
            prompt=PromptRegistry(
                (PromptSection("core", PromptStability.STATIC, lambda: "system"),)
            ),
            max_output_tokens=1000,
        ),
        ContextCompactor(SummaryModel()),
        rebuilder or PostCompactContextRebuilder((TodoPostCompactAttachmentSource(),)),
    )


def write_todos(
    session: Session,
    statuses: tuple[str, ...],
    *,
    failed: bool = False,
    dispatcher: bool = False,
) -> str:
    call_id = f"write-{len(session.conversation)}"
    arguments: JsonObject = {
        "todos": [
            {"content": f"task {i}", "status": status, "activeForm": f"doing {i}"}
            for i, status in enumerate(statuses)
        ]
    }
    call = ToolCall(
        call_id,
        "InvokeSearchedTool" if dispatcher else "TodoWrite",
        {"tool_name": "TodoWrite", "arguments": arguments} if dispatcher else arguments,
    )
    assistant = AssistantMessage(
        (call,), TokenUsage(), parent_uuid=session.causal_head_uuid
    )
    session.append_assistant_message(assistant)
    session.append_tool_results(
        (
            ToolResult(
                call_id, "result", ToolResultPresentation("result"), is_error=failed
            ),
        ),
        assistant,
    )
    return call_id


async def compact(
    session: Session, context: ContextEngine, trigger: CompactTrigger = "manual"
) -> tuple[AttachmentPayload, ...]:
    source = session.compaction_input()
    outcome = await context.compact(source, trigger)
    session.commit_compaction(
        outcome.replacements,
        outcome.summary,
        outcome.boundary,
        outcome.attachments,
        source=source,
    )
    return outcome.attachments


@pytest.mark.asyncio
@pytest.mark.parametrize("trigger", ["manual", "auto", "reactive"])
async def test_repeated_compact_restores_current_state_and_first_request(
    tmp_path: Path,
    trigger: CompactTrigger,
) -> None:
    session = Session(tmp_path, SESSION_ID)
    session.append_human_message(HumanMessage("work"))
    write_id = write_todos(session, ("completed", "in_progress"), dispatcher=True)
    session.append_attachment(
        SkillActivationAttachment("skill", "latest", "project", "skill")
    )
    tool = ToolDiscoveryDefinition("TodoWrite", "todo", {}, "fingerprint")
    session.append_attachment(ToolDiscoveryAttachment((tool,), "dispatcher"))
    session.append_attachment(CollaborationModeAttachment("plan"))
    session.append_attachment(FileMentionAttachment("old", "file body"))
    context = engine()
    original = session.conversation
    first = await compact(session, context, trigger)
    for _ in range(2):
        session = Session(tmp_path, SESSION_ID)
        assert await compact(session, context, trigger) == first
    assert first[0] == CollaborationModeAttachment("default")
    assert isinstance(first[1], InvokedSkillsAttachment)
    assert isinstance(first[-1], TodoSnapshotAttachment)
    assert first[-1].source_write_id == write_id
    assert tuple(todo.status for todo in first[-1].todos) == (
        "completed",
        "in_progress",
    )
    assert session.conversation[: len(original)] == original
    assert len(session.context_entries) == 5
    parent = session.context_entries[0].uuid
    for entry in session.context_entries[1:]:
        assert entry.parent_uuid == parent
        parent = entry.uuid
    plan = context.plan(
        session.context_planning_state(), SessionContextCache(), tools=()
    )
    assert any(origin.attachment_kind == "todo_snapshot" for origin in plan.provenance)
    assert "task 1" in str(plan.request.input)
    assert TodoReminderAttachmentSource()(session.attachment_derivation_state()) == ()
    for _ in range(10):
        session.append_assistant_message(
            AssistantMessage(
                (TextContent("working"),),
                TokenUsage(),
                parent_uuid=session.causal_head_uuid,
            )
        )
    assert (
        len(TodoReminderAttachmentSource()(session.attachment_derivation_state())) == 1
    )
    session.append_attachment(ToolDiscoveryInvalidationAttachment(("TodoWrite",)))
    session.append_attachment(
        SkillActivationAttachment("skill", "updated", "project", "skill")
    )
    write_todos(session, ())
    for _ in range(2):
        restored = await compact(session, context, trigger)
        assert not any(isinstance(p, ToolDiscoveryAttachment) for p in restored)
        assert isinstance(restored[1], InvokedSkillsAttachment)
        assert restored[1].skills[0].instructions == "updated"
        assert isinstance(restored[-1], TodoSnapshotAttachment)
        assert restored[-1].todos == ()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "statuses", [(), ("completed",), ("pending",), ("completed", "pending")]
)
async def test_todo_success_is_authority_even_before_boundary(
    tmp_path: Path,
    statuses: tuple[str, ...],
) -> None:
    session = Session(tmp_path, SESSION_ID)
    session.append_human_message(HumanMessage("work"))
    assert await compact(session, engine()) == (CollaborationModeAttachment("default"),)
    write_id = write_todos(session, statuses)
    await compact(session, engine())
    write_todos(session, ("in_progress",), failed=True)
    restored = await compact(session, engine())
    snapshot = restored[-1]
    assert isinstance(snapshot, TodoSnapshotAttachment)
    assert snapshot.source_write_id == write_id
    expected = () if all(s == "completed" for s in statuses) else statuses
    assert tuple(todo.status for todo in snapshot.todos) == expected
    assert project_todos(session.conversation).latest_write_id == write_id


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["head", "mode", "session"])
async def test_stale_proposal_does_not_commit(tmp_path: Path, change: str) -> None:
    session = Session(tmp_path, SESSION_ID)
    session.append_human_message(HumanMessage("work"))
    source = session.compaction_input()
    outcome = await engine().compact(source, "manual")
    if change == "head":
        session.append_human_message(
            HumanMessage("new", parent_uuid=session.causal_head_uuid)
        )
    elif change == "mode":
        session.set_collaboration_mode("plan")
    else:
        session = Session(tmp_path, "22222222-2222-2222-2222-222222222222")
    before = session.conversation
    with pytest.raises(ValueError, match="Stale"):
        session.commit_compaction(
            (), outcome.summary, outcome.boundary, outcome.attachments, source=source
        )
    assert session.conversation == before
    assert session.compact_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error", [RuntimeError("rebuild failed"), asyncio.CancelledError()]
)
async def test_rebuild_failure_or_cancel_leaves_old_working_set(
    tmp_path: Path,
    error: BaseException,
) -> None:
    session = Session(tmp_path, SESSION_ID)
    session.append_human_message(HumanMessage("work"))
    before = session.conversation

    def fail(state: CompactionInput) -> tuple[AttachmentPayload, ...]:
        raise error

    with pytest.raises(type(error)):
        await compact(session, engine(PostCompactContextRebuilder((fail,))))
    assert Session(tmp_path, SESSION_ID).conversation == before
    assert session.context_entries == before


@pytest.mark.asyncio
async def test_persistence_failure_preserves_whole_old_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = Session(tmp_path, SESSION_ID)
    session.append_human_message(HumanMessage("work"))
    write_todos(session, ("pending",))
    before = session.conversation

    def fail(*args: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(session._store, "_append_records", fail)
    with pytest.raises(OSError, match="disk full"):
        await compact(session, engine())
    assert session.context_entries == before
    assert Session(tmp_path, SESSION_ID).conversation == before


@pytest.mark.asyncio
@pytest.mark.parametrize("version", [6, 7])
async def test_legacy_history_is_not_rewritten_and_generates_v8_snapshot(
    tmp_path: Path,
    version: int,
) -> None:
    session = Session(tmp_path, SESSION_ID)
    session.append_human_message(HumanMessage("work"))
    write_todos(session, ("pending",))
    path = session._store.path
    records = [json.loads(line) for line in path.read_text().splitlines()]
    for record in records:
        record["schema_version"] = version
        if version == 6 and record["type"] == "session_started":
            for key in (
                "session_kind",
                "parent_session_id",
                "created_by_run_id",
                "agent_name",
            ):
                record.pop(key)
    original = "".join(json.dumps(record) + "\n" for record in records)
    path.write_text(original)
    session = Session(tmp_path, SESSION_ID)
    restored = await compact(session, engine())
    assert isinstance(restored[-1], TodoSnapshotAttachment)
    assert path.read_text().startswith(original)
    assert json.loads(path.read_text().splitlines()[-1])["schema_version"] == 8
    assert Session(tmp_path, SESSION_ID).context_entries == session.context_entries


@pytest.mark.asyncio
async def test_rebuilt_request_audit_and_history_preserve_attachment_identity(
    tmp_path: Path,
) -> None:
    from my_code.application.contracts.views import (
        TranscriptAttachment,
        TranscriptToolCall,
    )
    from my_code.application.sessions.transcript_projection import project_transcript
    from my_code.model.invocation import ModelInvocation, RequestPurpose

    session = Session(tmp_path, SESSION_ID)
    session.append_human_message(HumanMessage("work"))
    write_todos(session, ("pending",))
    context = engine()
    await compact(session, context)
    plan = context.plan(
        session.context_planning_state(), SessionContextCache(), tools=()
    )
    invocation = ModelInvocation(
        plan.request, plan.provenance, RequestPurpose.AGENT, session.causal_head_uuid, 1
    )
    session.prepare_model_invocation(invocation)
    session.finish_model_invocation(invocation.request_id, "completed")
    resumed = Session(tmp_path, SESSION_ID)
    audit = resumed.request_audit_snapshot().requests[-1]
    snapshot = resumed.context_entries[-1]
    assert isinstance(snapshot, AttachmentMessage)
    origin = audit.manifest.origins[-1]
    assert origin.source_id == snapshot.uuid
    assert origin.attachment_kind == "todo_snapshot"
    assert "task 0" in str(audit.input)
    history = project_transcript(resumed)
    assert sum(isinstance(entry, TranscriptToolCall) for entry in history.entries) == 1
    assert any(
        isinstance(entry, TranscriptAttachment)
        and entry.attachment_kind == "todo_snapshot"
        for entry in history.entries
    )


@pytest.mark.asyncio
async def test_summary_cancellation_preserves_original_session(tmp_path: Path) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    class BlockingModel:
        async def stream(
            self, request: ModelRequest
        ) -> AsyncIterator[ModelStreamEvent]:
            entered.set()
            await release.wait()
            async for event in SummaryModel().stream(request):
                yield event

    session = Session(tmp_path, SESSION_ID)
    session.append_human_message(HumanMessage("work"))
    before = session.conversation
    context = engine()
    context._compactor = ContextCompactor(BlockingModel())
    task = asyncio.create_task(compact(session, context))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert session.context_entries == before
    assert Session(tmp_path, SESSION_ID).conversation == before


@pytest.mark.asyncio
async def test_child_session_rebuild_does_not_capture_parent_todos(
    tmp_path: Path,
) -> None:
    from my_code.sessions.models import SessionStart

    parent = Session(tmp_path, SESSION_ID)
    parent.append_human_message(HumanMessage("parent"))
    write_todos(parent, ("pending",))
    child_id = "22222222-2222-2222-2222-222222222222"
    child = Session(
        tmp_path,
        child_id,
        start=SessionStart(
            child_id,
            "2026-09-12T00:00:00+00:00",
            str(tmp_path),
            "test",
            "test",
            "default",
            None,
            1000,
            session_kind="subagent",
            parent_session_id=parent.session_id,
            created_by_run_id="33333333-3333-3333-3333-333333333333",
            agent_name="child",
        ),
    )
    child.append_human_message(HumanMessage("child"))
    child.set_collaboration_mode("plan")
    context = engine()
    assert await compact(child, context) == (CollaborationModeAttachment("plan"),)
    assert isinstance((await compact(parent, context))[-1], TodoSnapshotAttachment)
    write_todos(child, ("completed",))
    child_attachments = await compact(child, context)
    assert isinstance(child_attachments[-1], TodoSnapshotAttachment)
    assert child_attachments[-1].todos == ()
    assert project_todos(parent.conversation).todos[0].status == "pending"


@pytest.mark.asyncio
async def test_restored_critical_state_is_not_truncated_to_hide_overflow(
    tmp_path: Path,
) -> None:
    from my_code.context.models import ContextOverflow

    session = Session(tmp_path, SESSION_ID)
    session.append_human_message(HumanMessage("work"))
    instructions = "critical instruction " * 50_000
    session.append_attachment(
        SkillActivationAttachment("large", instructions, "project", "large")
    )
    context = engine()
    restored = await compact(session, context)
    assert isinstance(restored[1], InvokedSkillsAttachment)
    assert restored[1].skills[0].instructions == instructions
    with pytest.raises(ContextOverflow):
        context.plan(session.context_planning_state(), SessionContextCache(), tools=())
    assert session.compact_count == 1
