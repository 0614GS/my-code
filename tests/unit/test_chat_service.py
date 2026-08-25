"""Concrete ChatService orchestration and session-bundle tests."""

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from my_code.agent.events import AgentEvent
from my_code.agent.models import AgentTurnInput, AgentTurnSucceeded
from my_code.auth.credentials import CredentialSource
from my_code.bootstrap import bootstrap_chat
from my_code.chat.history import HistoryText, HistoryToolCall
from my_code.chat.service import ChatService
from my_code.config.paths import MyCodePaths
from my_code.config.settings import AgentSettings
from my_code.context.models import CompactionOutcome
from my_code.conversation.attachments import TodoReminderAttachment
from my_code.conversation.models import (
    AssistantMessage,
    AttachmentMessage,
    ConversationSummaryMessage,
    HumanMessage,
    TextContent,
    ToolCall,
    ToolResult,
    ToolResultBatch,
)
from my_code.conversation.state import CompactBoundary
from my_code.model.primitives import TokenUsage
from my_code.permissions.models import PermissionMode
from my_code.providers.manager import ProviderUpdate
from my_code.sessions._store import SessionStore
from my_code.sessions.session import Session
from my_code.tools.presentation import ToolResultPresentation, ToolUsePresentation

_CURRENT_SESSION_ID = "11111111-1111-1111-1111-111111111111"
_TARGET_SESSION_ID = "22222222-2222-2222-2222-222222222222"


class CapturingAgent:
    def __init__(self) -> None:
        self.turn_input: AgentTurnInput | None = None

    async def submit(
        self,
        session: Session,
        turn_input: AgentTurnInput,
    ) -> AgentTurnSucceeded:
        del session
        self.turn_input = turn_input
        return AgentTurnSucceeded("done", 1, TokenUsage())


def _bootstrap_runtime(tmp_path: Path) -> ChatService:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    paths = MyCodePaths.discover(
        workspace,
        environ={},
        home=tmp_path / "home",
    )
    settings = AgentSettings(
        paths=paths,
        provider_id="anthropic",
        model="test-model",
        permission_mode=PermissionMode.DEFAULT,
        max_steps=3,
        max_output_tokens=1024,
        context_chars=10_000,
        interactive=True,
        credential_source=CredentialSource.NONE,
    )
    return bootstrap_chat(settings, _CURRENT_SESSION_ID)


def test_app_state_is_the_single_runtime_owner(tmp_path: Path) -> None:
    runtime = _bootstrap_runtime(tmp_path)

    assert not hasattr(runtime, "_active")
    assert not hasattr(runtime, "active_model_state")
    assert not hasattr(runtime, "provider_router")
    assert runtime.state.workspace.workspace is runtime.tool_executor.workspace
    assert runtime.state.permissions.policy is runtime.tool_executor.policy
    assert runtime.state.tools.catalog.snapshot() == runtime.tool_executor.tools
    assert runtime.state.session.session_id == _CURRENT_SESSION_ID


@pytest.mark.asyncio
async def test_runtime_loads_mentions_before_creating_turn_input(
    tmp_path: Path,
) -> None:
    runtime = _bootstrap_runtime(tmp_path)
    (runtime.settings.cwd / "context.txt").write_text("context", encoding="utf-8")
    agent = CapturingAgent()
    runtime.agent = agent  # type: ignore[assignment]

    await runtime.submit("review @context.txt")

    assert agent.turn_input is not None
    assert agent.turn_input.prompt == "review @context.txt"
    assert len(agent.turn_input.attachments) == 1
    assert agent.turn_input.attachments[0].kind == "file_mention"


@pytest.mark.asyncio
async def test_manual_compact_is_owned_and_committed_by_chat(tmp_path: Path) -> None:
    runtime = _bootstrap_runtime(tmp_path)
    active = runtime.state.session
    user = HumanMessage(content="compact this conversation")
    active.append_human_message(user)
    summary = ConversationSummaryMessage(
        content="continuation state",
        parent_uuid=user.uuid,
    )
    boundary = CompactBoundary(
        parent_uuid=user.uuid,
        summary_uuid=summary.uuid,
        trigger="manual",
        pre_compact_chars=10,
    )
    compact = AsyncMock(
        return_value=CompactionOutcome((), summary, boundary, TokenUsage(4, 2))
    )
    runtime.context.compact = compact  # type: ignore[method-assign]

    status = await runtime.compact()

    compact.assert_awaited_once()
    assert compact.await_args is not None
    snapshot, trigger = compact.await_args.args
    assert snapshot.messages == (user,)
    assert trigger == "manual"
    assert active.compact_count == 1
    assert active.snapshot().working_set == (summary,)
    assert status.compact_count == 1


@pytest.mark.asyncio
async def test_runtime_lists_and_atomically_switches_project_session(
    tmp_path: Path,
) -> None:
    runtime = _bootstrap_runtime(tmp_path)
    previous = runtime.state.session
    store = SessionStore(
        runtime.settings.paths.project_state_dir,
        _TARGET_SESSION_ID,
    )
    user = HumanMessage(content="historical question")
    assistant = AssistantMessage(
        content=(TextContent("historical answer"),),
        usage=TokenUsage(),
        parent_uuid=user.uuid,
    )
    store.append(user)
    store.append(assistant)

    sessions = await runtime.list_sessions()
    resumed = await runtime.resume_session(_TARGET_SESSION_ID)

    assert [session.session_id for session in sessions] == [_TARGET_SESSION_ID]
    assert resumed.status.session_id == _TARGET_SESSION_ID
    assert resumed.status.working_message_count == 2
    assert resumed.history == (
        HistoryText("user", "historical question"),
        HistoryText("assistant", "historical answer"),
    )
    assert runtime.status().session_id == _TARGET_SESSION_ID
    assert runtime.state.session is not previous
    assert not any(
        isinstance(message, AttachmentMessage)
        for message in runtime.state.session.context_snapshot().messages
    )
    assert runtime.state.session.session_id == _TARGET_SESSION_ID


@pytest.mark.asyncio
async def test_provider_switch_does_not_modify_session_facts(tmp_path: Path) -> None:
    runtime = _bootstrap_runtime(tmp_path)
    session = runtime.state.session
    session.append_human_message(HumanMessage("keep canonical facts"))
    before = session.snapshot()

    status = await runtime.configure_provider(
        ProviderUpdate("other", "other-model", None)
    )

    assert runtime.state.session is session
    assert session.snapshot() == before
    assert status.provider_id == "other"
    assert status.model == "other-model"
    assert runtime.state.provider.environment().descriptor.id == "other-model"


@pytest.mark.asyncio
async def test_resume_uses_persisted_tool_presentation_snapshot(tmp_path: Path) -> None:
    runtime = _bootstrap_runtime(tmp_path)
    store = SessionStore(
        runtime.settings.paths.project_state_dir,
        _TARGET_SESSION_ID,
    )
    user = HumanMessage(content="read it")
    assistant = AssistantMessage(
        content=(ToolCall("read-1", "Read", {"path": "old.py"}),),
        usage=TokenUsage(),
        parent_uuid=user.uuid,
    )
    snapshot = ToolResultPresentation(
        summary="Historical read summary",
        detail="Stored at execution time",
    )
    result = ToolResultBatch(
        content=(
            ToolResult(
                "read-1",
                "model-visible historical content",
            ),
        ),
        parent_uuid=assistant.uuid,
        source_assistant_id=assistant.uuid,
    )
    store.append(user)
    store.append(assistant)
    store.append_message(result, (("read-1", snapshot),))

    resumed = await runtime.resume_session(_TARGET_SESSION_ID)

    assert resumed.history == (
        HistoryText("user", "read it"),
        HistoryToolCall(
            tool_use_id="read-1",
            use=ToolUsePresentation(
                display_name="Read",
                summary="old.py",
                activity="Reading old.py",
            ),
            result=snapshot,
            is_error=False,
        ),
    )


@pytest.mark.asyncio
async def test_resume_projects_todos_into_runtime_status(tmp_path: Path) -> None:
    runtime = _bootstrap_runtime(tmp_path)
    store = SessionStore(
        runtime.settings.paths.project_state_dir,
        _TARGET_SESSION_ID,
    )
    user = HumanMessage(content="track it")
    assistant = AssistantMessage(
        content=(
            ToolCall(
                "todo-1",
                "TodoWrite",
                {
                    "todos": [
                        {
                            "content": "Run tests",
                            "status": "in_progress",
                            "activeForm": "Running tests",
                        }
                    ]
                },
            ),
        ),
        usage=TokenUsage(),
        parent_uuid=user.uuid,
    )
    result = ToolResultBatch(
        content=(ToolResult("todo-1", "updated"),),
        parent_uuid=assistant.uuid,
        source_assistant_id=assistant.uuid,
    )
    for message in (user, assistant, result):
        store.append(message)

    resumed = await runtime.resume_session(_TARGET_SESSION_ID)

    assert len(resumed.status.todos) == 1
    assert resumed.status.todos[0].active_form == "Running tests"


@pytest.mark.asyncio
async def test_failed_resume_keeps_the_complete_active_session_bundle(
    tmp_path: Path,
) -> None:
    runtime = _bootstrap_runtime(tmp_path)
    active = runtime.state.session
    anchor = HumanMessage(content="keep this session")
    active.append_human_message(anchor)
    active.append_attachment(TodoReminderAttachment("keep"))
    empty_id = "33333333-3333-3333-3333-333333333333"
    SessionStore(runtime.settings.paths.project_state_dir, empty_id).load()

    with pytest.raises(ValueError, match="contains no messages"):
        await runtime.resume_session(empty_id)

    assert runtime.state.session is active
    assert runtime.status().session_id == _CURRENT_SESSION_ID
    assert any(
        isinstance(message, AttachmentMessage)
        for message in runtime.state.session.context_snapshot().messages
    )


@pytest.mark.asyncio
async def test_stream_prevents_session_switch_until_turn_finishes(
    tmp_path: Path,
) -> None:
    runtime = _bootstrap_runtime(tmp_path)
    target = SessionStore(runtime.settings.paths.project_state_dir, _TARGET_SESSION_ID)
    target.append(HumanMessage(content="target"))
    started = asyncio.Event()
    release = asyncio.Event()

    class BlockingAgent:
        async def stream(
            self,
            session: Session,
            turn_input: AgentTurnInput,
        ) -> AsyncIterator[AgentEvent]:
            del session, turn_input
            started.set()
            await release.wait()
            yield AgentTurnSucceeded("done", 1, TokenUsage())

    runtime.agent = BlockingAgent()  # type: ignore[assignment]

    async def consume() -> None:
        async for _ in runtime.stream("wait"):
            pass

    turn = asyncio.create_task(consume())
    await started.wait()
    resume = asyncio.create_task(runtime.resume_session(_TARGET_SESSION_ID))
    await asyncio.sleep(0)
    assert not resume.done()

    release.set()
    await turn
    await resume
    assert runtime.status().session_id == _TARGET_SESSION_ID
