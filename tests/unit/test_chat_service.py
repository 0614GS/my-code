"""Concrete ChatService orchestration and session-bundle tests."""

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from nano_code.agent import (
    AgentEvent,
    AgentTurnCompleted,
    AgentTurnInput,
    AgentTurnSucceeded,
)
from nano_code.auth import CredentialSource
from nano_code.bootstrap import bootstrap_chat
from nano_code.chat import (
    ChatService,
    HistoryAssistantMessage,
    HistoryToolCall,
    HistoryUserMessage,
)
from nano_code.config import AgentSettings, NanoCodePaths
from nano_code.context import (
    AttachmentDelivery,
    ContextAttachment,
    ContextObservation,
    ContextSession,
)
from nano_code.conversation import (
    AssistantMessage,
    HumanMessage,
    TextContent,
    ToolCall,
    ToolResult,
    ToolResultsMessage,
)
from nano_code.model import TokenUsage
from nano_code.permissions import PermissionMode
from nano_code.sessions import Session, SessionStore
from nano_code.tools import (
    ToolResultPresentation,
    ToolResultStore,
    ToolUsePresentation,
)

_CURRENT_SESSION_ID = "11111111-1111-1111-1111-111111111111"
_TARGET_SESSION_ID = "22222222-2222-2222-2222-222222222222"


class CapturingAgent:
    def __init__(self) -> None:
        self.turn_input: AgentTurnInput | None = None

    async def submit(
        self,
        session: Session,
        context: ContextSession,
        result_store: ToolResultStore,
        turn_input: AgentTurnInput,
    ) -> AgentTurnSucceeded:
        del session, context, result_store
        self.turn_input = turn_input
        return AgentTurnSucceeded("done", 1, TokenUsage())


def _bootstrap_runtime(tmp_path: Path) -> ChatService:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    paths = NanoCodePaths.discover(
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
    assert agent.turn_input.attachments[0].retention == "live_session"


@pytest.mark.asyncio
async def test_runtime_lists_and_atomically_switches_project_session(
    tmp_path: Path,
) -> None:
    runtime = _bootstrap_runtime(tmp_path)
    previous = runtime._active
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
        HistoryUserMessage("historical question"),
        HistoryAssistantMessage("historical answer"),
    )
    assert runtime.status().session_id == _TARGET_SESSION_ID
    assert runtime._active is not previous
    assert runtime._active.context is not previous.context
    assert runtime._active.tool_results.root == runtime.settings.paths.tool_results_dir(
        _TARGET_SESSION_ID
    )


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
    result = ToolResultsMessage(
        content=(
            ToolResult(
                "read-1",
                "model-visible historical content",
            ),
        ),
        parent_uuid=assistant.uuid,
        source_assistant_uuid=assistant.uuid,
    )
    store.append(user)
    store.append(assistant)
    store.append_message(result, (("read-1", snapshot),))

    resumed = await runtime.resume_session(_TARGET_SESSION_ID)

    assert resumed.history == (
        HistoryUserMessage("read it"),
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
    result = ToolResultsMessage(
        content=(ToolResult("todo-1", "updated"),),
        parent_uuid=assistant.uuid,
        source_assistant_uuid=assistant.uuid,
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
    active = runtime._active
    anchor = HumanMessage(content="keep this session")
    active.session.append(anchor)
    active.context.add(
        (
            AttachmentDelivery(
                anchor.uuid,
                ContextAttachment(
                    "file",
                    (ContextObservation("File: keep.txt", "keep"),),
                    retention="live_session",
                ),
            ),
        ),
        active.session.conversation.snapshot(),
    )
    empty_id = "33333333-3333-3333-3333-333333333333"
    SessionStore(runtime.settings.paths.project_state_dir, empty_id).load()

    with pytest.raises(ValueError, match="contains no messages"):
        await runtime.resume_session(empty_id)

    assert runtime._active is active
    assert runtime.status().session_id == _CURRENT_SESSION_ID
    assert runtime._active.tool_results is active.tool_results
    assert runtime._active.context.snapshot(
        active.session.conversation.snapshot()
    ).attachment_deliveries


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
            context: ContextSession,
            result_store: ToolResultStore,
            turn_input: AgentTurnInput,
        ) -> AsyncIterator[AgentEvent]:
            del session, context, result_store, turn_input
            started.set()
            await release.wait()
            yield AgentTurnCompleted(AgentTurnSucceeded("done", 1, TokenUsage()))

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
