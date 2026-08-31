"""Concrete ApplicationService orchestration and session-bundle tests."""

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from my_code.agent.events import (
    AgentConversationUpdated,
    AgentEvent,
    AgentInputAccepted,
)
from my_code.agent.models import AgentInvocationSucceeded, AgentTurnInput
from my_code.application.contracts.events import (
    BackgroundInvocationFinished,
    BackgroundInvocationStarted,
    CompactionCompleted,
    CompactionStarted,
    ContextUpdated,
    TodoListUpdated,
    TurnInputAccepted,
    TurnSucceeded,
)
from my_code.application.contracts.history import (
    HistoryContextGroup,
    HistoryContextItem,
    HistoryText,
    HistoryToolCall,
)
from my_code.application.contracts.views import (
    TranscriptAttachment,
    TranscriptReasoning,
    TranscriptSummary,
    TranscriptText,
    TranscriptToolCall,
    TranscriptToolResult,
)
from my_code.application.service import ApplicationService
from my_code.application.turns.event_projection import project_agent_events
from my_code.auth.credentials import CredentialSource, CredentialStore
from my_code.bootstrap import bootstrap_application
from my_code.config.paths import MyCodePaths
from my_code.config.providers import ProviderProtocol
from my_code.config.settings import AgentSettings, SandboxMode
from my_code.context.models import CompactionOutcome
from my_code.context.session_cache import SessionContextCache
from my_code.conversation.attachments import (
    FileMentionAttachment,
    TodoReminderAttachment,
)
from my_code.conversation.models import (
    AssistantMessage,
    AttachmentMessage,
    ConversationSummaryMessage,
    HumanMessage,
    ReasoningContent,
    TextContent,
    ToolCall,
    ToolResult,
    ToolResultBatch,
)
from my_code.conversation.presentation import (
    FileDiffHunk,
    FileDiffLine,
    FileDiffPresentation,
    ToolResultPresentation,
)
from my_code.conversation.state import CompactBoundary
from my_code.features.background_tasks.registry import BackgroundTask
from my_code.features.background_tasks.wake import BackgroundTaskWakeSignal
from my_code.foundation.json import JsonObject
from my_code.model.capabilities import ModelDescriptor, ModelLimits
from my_code.model.invocation import (
    ModelInputOrigin,
    ModelInputOriginKind,
    ModelInvocation,
    RequestPurpose,
)
from my_code.model.primitives import ReasoningPresentation, TokenUsage
from my_code.model.request import InputText, ModelRequest, SystemPrompt, UserInput
from my_code.permissions.models import PermissionMode
from my_code.providers.discovery import ModelDiscoveryService
from my_code.providers.manager import ProviderManager, ProviderUpdate
from my_code.sessions._store import SessionStore
from my_code.sessions.session import Session
from my_code.tools.presentation import ToolUsePresentation

_CURRENT_SESSION_ID = "11111111-1111-1111-1111-111111111111"
_TARGET_SESSION_ID = "22222222-2222-2222-2222-222222222222"


class CapturingAgent:
    def __init__(self) -> None:
        self.turn_input: AgentTurnInput | None = None

    async def submit(
        self,
        session: Session,
        runtime: SessionContextCache,
        turn_input: AgentTurnInput,
    ) -> AgentInvocationSucceeded:
        del session, runtime
        self.turn_input = turn_input
        return AgentInvocationSucceeded("done", 1, TokenUsage())


class InteractiveCapturingAgent:
    async def stream_continuation(
        self,
        session: Session,
        runtime: SessionContextCache,
        pending_source=None,
    ) -> AsyncIterator[AgentEvent]:
        del runtime
        assert pending_source is not None
        inputs = await pending_source.drain_pending()
        session.commit_user_inputs((item.prompt, item.attachments) for item in inputs)
        ids = tuple(item.input_id for item in inputs if item.input_id is not None)
        pending_source.accept_pending(ids)
        for item in inputs:
            yield AgentInputAccepted(item.input_id, item.prompt)
        yield AgentInvocationSucceeded("done", 1, TokenUsage())


def _bootstrap_runtime(
    tmp_path: Path, permission_mode: PermissionMode = PermissionMode.DEFAULT
) -> ApplicationService:
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
        permission_mode=permission_mode,
        max_steps=3,
        max_output_tokens=1024,
        interactive=True,
        sandbox_mode=SandboxMode.LOCAL,
        credential_source=CredentialSource.NONE,
    )
    return bootstrap_application(settings, _CURRENT_SESSION_ID)


def test_app_state_is_the_single_runtime_owner(tmp_path: Path) -> None:
    runtime = _bootstrap_runtime(tmp_path)

    assert not hasattr(runtime, "_active")
    assert not hasattr(runtime, "active_model_state")
    assert not hasattr(runtime, "provider_router")
    assert runtime.runtime.workspace is runtime.tool_executor.workspace
    assert runtime.runtime.permissions.policy is runtime.tool_executor.policy
    assert runtime.runtime.tools.snapshot() == runtime.tool_executor.tools
    assert runtime.runtime.session.session_id == _CURRENT_SESSION_ID


@pytest.mark.asyncio
async def test_interactive_stream_accepts_queued_inputs_before_scrollback(
    tmp_path: Path,
) -> None:
    runtime = _bootstrap_runtime(tmp_path)
    runtime.agent = InteractiveCapturingAgent()  # type: ignore[assignment]
    first = runtime.queue_input("first")
    second = runtime.queue_input("second")

    events = [event async for event in runtime.stream_interactive()]

    accepted = [event for event in events if isinstance(event, TurnInputAccepted)]
    assert [(event.input_id, event.prompt) for event in accepted] == [
        (first.input_id, "first"),
        (second.input_id, "second"),
    ]
    assert runtime.queued_inputs() == ()
    assert [
        item.content
        for item in runtime.runtime.session.conversation
        if isinstance(item, HumanMessage)
    ] == ["first", "second"]


@pytest.mark.asyncio
async def test_committed_model_step_projects_a_context_snapshot(tmp_path: Path) -> None:
    runtime = _bootstrap_runtime(tmp_path)
    runtime.runtime.session.append_human_message(HumanMessage("inspect context"))

    async def agent_events() -> AsyncIterator[AgentEvent]:
        yield AgentConversationUpdated()

    events = [
        event
        async for event in project_agent_events(
            runtime.runtime.session, agent_events(), runtime.context_status
        )
    ]

    assert len(events) == 1
    assert isinstance(events[0], ContextUpdated)
    assert events[0].status.context_entry_count == 1
    assert events[0].status.conversation_entry_count == 1


@pytest.mark.asyncio
async def test_accepted_input_creates_the_first_context_snapshot(
    tmp_path: Path,
) -> None:
    runtime = _bootstrap_runtime(tmp_path)
    runtime.runtime.session.append_human_message(HumanMessage("first prompt"))

    async def agent_events() -> AsyncIterator[AgentEvent]:
        yield AgentInputAccepted("input-1", "first prompt")

    events = [
        event
        async for event in project_agent_events(
            runtime.runtime.session, agent_events(), runtime.context_status
        )
    ]

    assert isinstance(events[0], TurnInputAccepted)
    assert isinstance(events[1], ContextUpdated)


@pytest.mark.asyncio
async def test_input_batch_creates_only_one_context_snapshot(tmp_path: Path) -> None:
    runtime = _bootstrap_runtime(tmp_path)
    runtime.runtime.session.commit_user_inputs((("first", ()), ("second", ())))

    async def agent_events() -> AsyncIterator[AgentEvent]:
        yield AgentInputAccepted("input-1", "first")
        yield AgentInputAccepted("input-2", "second")

    events = [
        event
        async for event in project_agent_events(
            runtime.runtime.session, agent_events(), runtime.context_status
        )
    ]

    assert sum(isinstance(event, ContextUpdated) for event in events) == 1


@pytest.mark.asyncio
async def test_dispatcher_todo_write_always_projects_its_completed_snapshot(
    tmp_path: Path,
) -> None:
    runtime = _bootstrap_runtime(tmp_path)
    session = runtime.runtime.session
    session.append_human_message(HumanMessage("show completed todos"))
    todo_input: JsonObject = {
        "todos": [
            {
                "content": "Run tests",
                "status": "completed",
                "activeForm": "Running tests",
            }
        ]
    }

    async def agent_events() -> AsyncIterator[AgentEvent]:
        for index in range(2):
            assistant = AssistantMessage(
                (
                    ToolCall(
                        f"todo-{index}",
                        "InvokeSearchedTool",
                        {"tool_name": "TodoWrite", "arguments": todo_input},
                    ),
                ),
                TokenUsage(),
                parent_uuid=session.conversation[-1].uuid,
            )
            session.append_assistant_message(assistant)
            session.commit_tool_round(
                ToolResultBatch(
                    (
                        ToolResult(
                            f"todo-{index}",
                            "updated",
                            ToolResultPresentation("Updated 1 todo(s)"),
                        ),
                    ),
                    assistant.uuid,
                    parent_uuid=assistant.uuid,
                )
            )
            yield AgentConversationUpdated()

    events = [
        event
        async for event in project_agent_events(
            session, agent_events(), runtime.context_status
        )
    ]
    updates = [event for event in events if isinstance(event, TodoListUpdated)]

    assert len(updates) == 2
    assert updates[0].todos[0].status == "completed"
    assert updates[1].todos[0].content == "Run tests"


def test_runtime_permission_modes_cycle_without_persisting_settings(
    tmp_path: Path,
) -> None:
    runtime = _bootstrap_runtime(tmp_path)
    runtime.runtime.session.append_human_message(HumanMessage("persist mode"))
    configured = runtime.settings.permission_mode

    assert [item.display_name for item in runtime.permission_modes()] == [
        "Ask for me",
        "Approve edits",
        "Full access",
    ]
    selected = runtime.select_permission_mode("acceptEdits")
    assert selected.changed is True
    assert selected.requires_confirmation is False
    assert runtime.status().permission_mode == "acceptEdits"
    pending_direct = runtime.select_permission_mode("bypassPermissions")
    assert pending_direct.changed is False
    assert pending_direct.requires_confirmation is True
    runtime.confirm_full_access(False)
    runtime.select_permission_mode("default")
    assert runtime.cycle_permission_mode().mode.value == "acceptEdits"
    pending = runtime.cycle_permission_mode()
    assert pending.requires_confirmation is True
    assert runtime.status().permission_mode == "acceptEdits"
    runtime.confirm_full_access(True)
    assert runtime.status().permission_mode == "bypassPermissions"
    assert runtime.cycle_permission_mode().mode.value == "default"
    assert runtime.settings.permission_mode is configured
    assert (
        Session.restore(
            runtime.settings.paths.project_state_dir, _CURRENT_SESSION_ID
        ).permission_mode
        == "default"
    )


@pytest.mark.parametrize("mode", [PermissionMode.PLAN, PermissionMode.DONT_ASK])
def test_non_carousel_permission_mode_cycles_back_to_default(
    tmp_path: Path, mode: PermissionMode
) -> None:
    runtime = _bootstrap_runtime(tmp_path)
    runtime.runtime.permissions.policy.mode = mode

    switched = runtime.cycle_permission_mode()

    assert switched.changed is True
    assert runtime.runtime.permissions.policy.mode is PermissionMode.DEFAULT


def test_full_access_confirmation_is_per_process_and_skipped_in_sandbox(
    tmp_path: Path,
) -> None:
    runtime = _bootstrap_runtime(tmp_path)
    runtime.runtime.permissions.policy.mode = PermissionMode.ACCEPT_EDITS

    assert runtime.cycle_permission_mode().requires_confirmation is True
    runtime.confirm_full_access(True)
    runtime.cycle_permission_mode()
    runtime.cycle_permission_mode()
    runtime.cycle_permission_mode()
    assert runtime.status().permission_mode == "bypassPermissions"

    runtime.runtime.permissions.full_access_confirmed = False
    runtime.runtime.permissions.sandbox_active = True
    runtime.runtime.permissions.policy.mode = PermissionMode.ACCEPT_EDITS
    switched = runtime.cycle_permission_mode()
    assert switched.requires_confirmation is False
    assert runtime.status().permission_mode == "bypassPermissions"


def test_rejecting_bypass_startup_falls_back_to_default(tmp_path: Path) -> None:
    runtime = _bootstrap_runtime(tmp_path, PermissionMode.BYPASS)

    assert runtime.current_permission_mode().requires_confirmation is True
    assert runtime.status().permission_mode == "default"
    current = runtime.confirm_full_access(False)

    assert current.value == "default"
    assert runtime.runtime.session.permission_mode == "default"


def test_permission_mode_write_failure_preserves_runtime_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = _bootstrap_runtime(tmp_path)

    def fail(_: str) -> bool:
        raise OSError("disk full")

    monkeypatch.setattr(runtime.runtime.session, "set_permission_mode", fail)
    with pytest.raises(OSError, match="disk full"):
        runtime.cycle_permission_mode()

    assert runtime.status().permission_mode == "default"


def test_full_access_confirmation_cannot_elevate_without_pending_switch(
    tmp_path: Path,
) -> None:
    runtime = _bootstrap_runtime(tmp_path)

    current = runtime.confirm_full_access(True)

    assert current.value == "default"
    assert runtime.status().permission_mode == "default"


@pytest.mark.asyncio
async def test_background_watcher_runs_continuation_without_human_message(
    tmp_path: Path,
) -> None:
    runtime = _bootstrap_runtime(tmp_path)
    runtime.runtime.session.append_human_message(HumanMessage("original"))
    signal = BackgroundTaskWakeSignal()

    class PendingSource:
        pending = True

        def has_pending(self, owner_run_id: str) -> bool:
            assert owner_run_id == _CURRENT_SESSION_ID
            return self.pending

    source = PendingSource()

    class ContinuationAgent:
        calls = 0

        async def stream_continuation(
            self,
            session: Session,
            context_runtime: SessionContextCache,
            pending_source=None,
        ) -> AsyncIterator[AgentEvent]:
            del context_runtime, pending_source
            self.calls += 1
            source.pending = False
            assert (
                sum(isinstance(item, HumanMessage) for item in session.conversation)
                == 1
            )
            yield AgentInvocationSucceeded(
                "handled", 1, TokenUsage(2, 1, provider_reported=True)
            )

    agent = ContinuationAgent()
    runtime.agent = agent  # type: ignore[assignment]
    runtime.background_notifications = source  # type: ignore[assignment]
    runtime.background_wake_signal = signal
    stream = runtime.stream_background_notifications()

    events = [await anext(stream), await anext(stream), await anext(stream)]

    assert isinstance(events[0], BackgroundInvocationStarted)
    assert events[1] == TurnSucceeded("handled", 1, 2, 1)
    assert events[2] == BackgroundInvocationFinished()
    assert agent.calls == 1


@pytest.mark.asyncio
async def test_failed_background_continuation_waits_for_a_new_revision(
    tmp_path: Path,
) -> None:
    runtime = _bootstrap_runtime(tmp_path)
    signal = BackgroundTaskWakeSignal()

    class PendingSource:
        def has_pending(self, owner_run_id: str) -> bool:
            del owner_run_id
            return True

    class FailingAgent:
        async def stream_continuation(
            self,
            session: Session,
            context_runtime: SessionContextCache,
            pending_source=None,
        ) -> AsyncIterator[AgentEvent]:
            del session, context_runtime, pending_source
            raise RuntimeError("provider unavailable")
            yield  # pragma: no cover

    runtime.agent = FailingAgent()  # type: ignore[assignment]
    runtime.background_notifications = PendingSource()  # type: ignore[assignment]
    runtime.background_wake_signal = signal
    stream = runtime.stream_background_notifications()

    assert isinstance(await anext(stream), BackgroundInvocationStarted)
    finished = await anext(stream)
    assert finished == BackgroundInvocationFinished("provider unavailable")

    async def receive_next_event():
        return await anext(stream)

    next_event = asyncio.create_task(receive_next_event())
    await asyncio.sleep(0)
    assert not next_event.done()
    signal.pulse()
    assert isinstance(
        await asyncio.wait_for(next_event, 1), BackgroundInvocationStarted
    )


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
    active = runtime.runtime.session
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
        pre_compact_tokens=10,
        measurement="estimated",
    )
    compact = AsyncMock(
        return_value=CompactionOutcome(
            (), summary, boundary, TokenUsage(4, 2, provider_reported=True)
        )
    )
    runtime.context.compact = compact  # type: ignore[method-assign]

    status = await runtime.compact()

    compact.assert_awaited_once()
    assert compact.await_args is not None
    state, trigger = compact.await_args.args
    assert state.context_entries == (user,)
    assert trigger == "manual"
    assert active.compact_count == 1
    assert active.context_entries == (summary,)
    assert status.compact_count == 1


@pytest.mark.asyncio
async def test_manual_compaction_stream_exposes_committed_lifecycle(
    tmp_path: Path,
) -> None:
    runtime = _bootstrap_runtime(tmp_path)
    active = runtime.runtime.session
    user = HumanMessage(content="compact with progress")
    active.append_human_message(user)
    summary = ConversationSummaryMessage(
        content="continuation state",
        parent_uuid=user.uuid,
    )
    boundary = CompactBoundary(
        parent_uuid=user.uuid,
        summary_uuid=summary.uuid,
        trigger="manual",
        pre_compact_tokens=10,
        measurement="estimated",
    )
    runtime.context.compact = AsyncMock(  # type: ignore[method-assign]
        return_value=CompactionOutcome(
            (), summary, boundary, TokenUsage(4, 2, provider_reported=True)
        )
    )

    events = [event async for event in runtime.stream_compaction()]

    assert events[0] == CompactionStarted("manual")
    assert isinstance(events[1], CompactionCompleted)
    assert events[1].trigger == "manual"
    assert events[1].usage == TokenUsage(4, 2, provider_reported=True)
    assert events[1].status.compact_count == 1
    assert active.compact_count == 1


@pytest.mark.asyncio
async def test_initialize_and_usage_return_safe_frontend_snapshots(
    tmp_path: Path,
) -> None:
    runtime = _bootstrap_runtime(tmp_path)
    user = HumanMessage("measure this")
    runtime.runtime.session.append_human_message(user)
    runtime.runtime.session.append_assistant_message(
        AssistantMessage(
            (TextContent("measured"),),
            TokenUsage(11, 3, 5, 7, True),
            parent_uuid=user.uuid,
        )
    )

    view = await runtime.initialize()
    usage = runtime.session_usage()
    capabilities = runtime.capabilities()

    assert view.status.session_id == _CURRENT_SESSION_ID
    assert view.history[:2] == (
        HistoryText("user", "measure this"),
        HistoryText("assistant", "measured", is_final_answer=True),
    )
    assert usage.request_count == 1
    assert usage.input_tokens == 11
    assert usage.cache_creation_input_tokens == 5
    assert usage.cache_read_input_tokens == 7
    assert usage.total_input_tokens == 23
    assert usage.output_tokens == 3
    assert all(isinstance(tool.name, str) for tool in capabilities.tools)
    assert runtime.runtime.skills.started is True
    assert runtime.runtime.mcp.started is True
    await runtime.close()


@pytest.mark.asyncio
async def test_initialize_uses_persisted_model_metadata_without_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = _bootstrap_runtime(tmp_path)

    async def resolve(*args: object, **kwargs: object):
        del args, kwargs
        raise AssertionError("startup must not perform model discovery")

    monkeypatch.setattr(ModelDiscoveryService, "resolve", resolve)

    await runtime.initialize()

    descriptor = runtime.runtime.provider.environment().descriptor
    assert descriptor.id == "test-model"
    assert runtime.runtime.session.start.model_limits == descriptor.limits
    assert runtime.runtime.session.start.model_limit_source == descriptor.source.value
    await runtime.close()


@pytest.mark.asyncio
async def test_local_model_switch_persists_and_updates_runtime(
    tmp_path: Path,
) -> None:
    runtime = _bootstrap_runtime(tmp_path)
    await runtime.configure_provider(
        ProviderUpdate(
            "other",
            ProviderProtocol.ANTHROPIC_MESSAGES,
            "first-model",
            None,
            models=(
                ModelDescriptor("first-model"),
                ModelDescriptor(
                    "second-model", limits=ModelLimits(max_input_tokens=999)
                ),
            ),
        )
    )
    await runtime.select_model("second-model")

    assert runtime.status().provider_id == "other"
    assert runtime.status().model == "second-model"
    assert runtime.runtime.provider.environment().descriptor.id == "second-model"
    assert runtime.provider_manager.resolve("other").model == "second-model"
    await runtime.close()


@pytest.mark.asyncio
async def test_background_task_view_is_owner_scoped(tmp_path: Path) -> None:
    runtime = _bootstrap_runtime(tmp_path)
    registry = runtime.activity.background_registry
    assert registry is not None
    owned_id = str(uuid4())
    foreign_id = str(uuid4())
    owned = BackgroundTask(
        owned_id,
        _CURRENT_SESSION_ID,
        "bash",
        "safe summary",
        {"output_file": "/private/owned.output"},
    )
    foreign = BackgroundTask(
        foreign_id,
        "another-session",
        "subagent",
        "must not leak",
    )
    registry.register(owned)
    registry.register(foreign)

    async def finish() -> object:
        return None

    owned_handle = await registry.tasks.submit(finish, name="owned", task_id=owned_id)
    foreign_handle = await registry.tasks.submit(
        finish, name="foreign", task_id=foreign_id
    )
    await owned_handle.wait()
    await foreign_handle.wait()

    views = runtime.background_tasks()

    assert [(item.task_id, item.output_path) for item in views] == [
        (owned_id, "/private/owned.output")
    ]
    assert all("must not leak" not in item.summary for item in views)
    await runtime.close()


@pytest.mark.asyncio
async def test_runtime_lists_and_atomically_switches_project_session(
    tmp_path: Path,
) -> None:
    runtime = _bootstrap_runtime(tmp_path)
    previous = runtime.runtime.session
    previous_binding = runtime.runtime.foreground
    previous_cache = runtime.runtime.context_cache
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
    store.set_permission_mode("acceptEdits")

    sessions = await runtime.list_sessions()
    resumed = await runtime.resume_session(_TARGET_SESSION_ID)

    assert [session.session_id for session in sessions] == [_TARGET_SESSION_ID]
    assert resumed.status.session_id == _TARGET_SESSION_ID
    assert resumed.status.context_entry_count == 2
    assert resumed.status.permission_mode == "acceptEdits"
    assert resumed.history == (
        HistoryText("user", "historical question"),
        HistoryText("assistant", "historical answer", is_final_answer=True),
    )
    assert runtime.status().session_id == _TARGET_SESSION_ID
    assert runtime.runtime.session is not previous
    assert runtime.runtime.foreground is not previous_binding
    assert runtime.runtime.context_cache is not previous_cache
    assert runtime.runtime.session.run_id != runtime.runtime.session.session_id
    assert not any(
        isinstance(message, AttachmentMessage)
        for message in runtime.runtime.session.context_entries
    )
    assert runtime.runtime.session.session_id == _TARGET_SESSION_ID


@pytest.mark.asyncio
async def test_resume_restores_persisted_bypass_without_confirmation(
    tmp_path: Path,
) -> None:
    runtime = _bootstrap_runtime(tmp_path)
    store = SessionStore(
        runtime.settings.paths.project_state_dir,
        _TARGET_SESSION_ID,
    )
    store.append(HumanMessage("full access session"))
    store.set_permission_mode("bypassPermissions")

    resumed = await runtime.resume_session(_TARGET_SESSION_ID)

    assert resumed.status.permission_mode == "bypassPermissions"
    assert runtime.current_permission_mode().requires_confirmation is False
    assert runtime.runtime.permissions.full_access_confirmed is True


def test_startup_session_uses_saved_mode_and_cli_override_is_persisted(
    tmp_path: Path,
) -> None:
    initial = _bootstrap_runtime(tmp_path)
    store = SessionStore(
        initial.settings.paths.project_state_dir,
        _TARGET_SESSION_ID,
    )
    store.append(HumanMessage("resume at startup"))
    store.set_permission_mode("bypassPermissions")

    restored = bootstrap_application(initial.settings, _TARGET_SESSION_ID)
    assert restored.status().permission_mode == "bypassPermissions"
    assert restored.current_permission_mode().requires_confirmation is False

    overridden = bootstrap_application(
        initial.settings,
        _TARGET_SESSION_ID,
        permission_mode_override=PermissionMode.PLAN,
    )
    assert overridden.status().permission_mode == "plan"
    assert (
        Session.restore(
            initial.settings.paths.project_state_dir, _TARGET_SESSION_ID
        ).permission_mode
        == "plan"
    )

    bypass_override = bootstrap_application(
        initial.settings,
        _TARGET_SESSION_ID,
        permission_mode_override=PermissionMode.BYPASS,
    )
    assert bypass_override.status().permission_mode == "bypassPermissions"
    assert bypass_override.current_permission_mode().requires_confirmation is False
    assert (
        Session.restore(
            initial.settings.paths.project_state_dir, _TARGET_SESSION_ID
        ).permission_mode
        == "bypassPermissions"
    )


@pytest.mark.asyncio
async def test_session_switch_pulses_background_watcher_without_delivery(
    tmp_path: Path,
) -> None:
    runtime = _bootstrap_runtime(tmp_path)
    signal = BackgroundTaskWakeSignal()
    runtime.background_wake_signal = signal
    store = SessionStore(
        runtime.settings.paths.project_state_dir,
        _TARGET_SESSION_ID,
    )
    store.append(HumanMessage(content="target"))

    await runtime.resume_session(_TARGET_SESSION_ID)

    assert signal.revision == 1
    assert not any(
        isinstance(message, AttachmentMessage)
        for message in runtime.runtime.session.conversation
    )


@pytest.mark.asyncio
async def test_provider_switch_does_not_modify_session_facts(tmp_path: Path) -> None:
    runtime = _bootstrap_runtime(tmp_path)
    session = runtime.runtime.session
    session.append_human_message(HumanMessage("keep canonical facts"))
    before = session.conversation

    status = await runtime.configure_provider(
        ProviderUpdate(
            "other", ProviderProtocol.ANTHROPIC_MESSAGES, "other-model", None
        )
    )

    assert runtime.runtime.session is session
    assert session.conversation == before
    assert status.provider_id == "other"
    assert status.model == "other-model"
    assert runtime.runtime.provider.environment().descriptor.id == "other-model"


@pytest.mark.asyncio
async def test_removing_current_provider_key_rebinds_runtime(tmp_path: Path) -> None:
    runtime = _bootstrap_runtime(tmp_path)
    runtime.provider_manager.configure(
        ProviderUpdate(
            "anthropic",
            ProviderProtocol.ANTHROPIC_MESSAGES,
            "test-model",
            None,
            "stored-key",
        )
    )
    await runtime.runtime.provider.switch(
        runtime.provider_manager.resolve("anthropic"),
        runtime.runtime.provider.environment(),
    )

    status = await runtime.remove_provider_credential("anthropic")

    assert status.credential_source == "none"
    assert runtime.runtime.provider.router.connection.api_key is None
    assert (
        CredentialStore(runtime.settings.paths.credentials_path).load_api_key(
            "anthropic"
        )
        is None
    )


@pytest.mark.asyncio
async def test_removing_non_current_provider_key_does_not_switch_runtime(
    tmp_path: Path,
) -> None:
    runtime = _bootstrap_runtime(tmp_path)
    runtime.provider_manager.configure(
        ProviderUpdate(
            "other",
            ProviderProtocol.ANTHROPIC_MESSAGES,
            "other-model",
            None,
            "other-key",
        )
    )
    await runtime.configure_provider(
        ProviderUpdate(
            "anthropic",
            ProviderProtocol.ANTHROPIC_MESSAGES,
            "test-model",
            None,
        )
    )
    connection = runtime.runtime.provider.router.connection

    await runtime.remove_provider_credential("other")

    assert runtime.runtime.provider.router.connection is connection
    assert (
        CredentialStore(runtime.settings.paths.credentials_path).load_api_key("other")
        is None
    )


@pytest.mark.asyncio
async def test_removing_current_stored_key_ignores_environment_key(
    tmp_path: Path,
) -> None:
    runtime = _bootstrap_runtime(tmp_path)
    runtime.provider_manager = ProviderManager(
        runtime.settings.paths,
        environ={"ANTHROPIC_API_KEY": "environment-key"},
    )
    connection = runtime.provider_manager.configure(
        ProviderUpdate(
            "anthropic",
            ProviderProtocol.ANTHROPIC_MESSAGES,
            "test-model",
            None,
            "stored-key",
        )
    )
    await runtime.runtime.provider.switch(
        connection,
        runtime.runtime.provider.environment(),
    )

    status = await runtime.remove_provider_credential("anthropic")

    assert status.credential_source == "none"
    assert runtime.runtime.provider.router.connection.api_key is None
    assert "environment-key" not in repr(runtime.providers()[0])


@pytest.mark.asyncio
async def test_credential_delete_failure_preserves_connection_and_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = _bootstrap_runtime(tmp_path)
    connection = runtime.provider_manager.configure(
        ProviderUpdate(
            "anthropic",
            ProviderProtocol.ANTHROPIC_MESSAGES,
            "test-model",
            None,
            "stored-key",
        )
    )
    await runtime.runtime.provider.switch(
        connection,
        runtime.runtime.provider.environment(),
    )
    connection = runtime.runtime.provider.router.connection

    def fail(_provider_id: str) -> bool:
        raise OSError("disk unavailable")

    monkeypatch.setattr(runtime.provider_manager, "delete_credential", fail)

    with pytest.raises(OSError, match="disk unavailable"):
        await runtime.remove_provider_credential("anthropic")

    assert runtime.runtime.provider.router.connection is connection
    assert runtime.provider_manager.credentials.load_api_key("anthropic") == (
        "stored-key"
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
        content=(
            ToolCall(
                "edit-1",
                "Edit",
                {
                    "path": "old.py",
                    "old_string": "old",
                    "new_string": "new",
                },
            ),
        ),
        usage=TokenUsage(),
        parent_uuid=user.uuid,
    )
    snapshot = ToolResultPresentation(
        summary="Historical edit summary",
        detail="Stored at execution time",
        file_diff=FileDiffPresentation(
            "old.py",
            "updated",
            1,
            1,
            (
                FileDiffHunk(
                    1,
                    1,
                    1,
                    1,
                    (
                        FileDiffLine("deletion", "old", old_line=1),
                        FileDiffLine("addition", "new", new_line=1),
                    ),
                ),
            ),
        ),
    )
    result = ToolResultBatch(
        content=(
            ToolResult(
                "edit-1",
                "model-visible historical content",
                snapshot,
            ),
        ),
        parent_uuid=assistant.uuid,
        source_assistant_id=assistant.uuid,
    )
    store.append(user)
    store.append(assistant)
    store.append_message(result)

    resumed = await runtime.resume_session(_TARGET_SESSION_ID)

    assert resumed.history == (
        HistoryText("user", "read it"),
        HistoryToolCall(
            tool_use_id="edit-1",
            use=ToolUsePresentation(
                display_name="Edit",
                summary="old.py",
                activity="Editing old.py",
                category="change",
            ),
            result=snapshot,
            is_error=False,
            ends_tool_batch=True,
            name="Edit",
            input={
                "path": "old.py",
                "old_string": "old",
                "new_string": "new",
            },
        ),
    )


def test_session_history_recovers_detailed_injections_from_request_audit(
    tmp_path: Path,
) -> None:
    runtime = _bootstrap_runtime(tmp_path)
    session = runtime.runtime.session
    session.commit_user_inputs(
        (("inspect notes", (FileMentionAttachment("notes.txt", "secret notes"),)),)
    )
    invocation = ModelInvocation(
        request=ModelRequest(
            SystemPrompt.from_text("system"),
            (UserInput((InputText("secret notes"),)),),
            (),
            128,
        ),
        origins=(
            ModelInputOrigin(
                ModelInputOriginKind.ATTACHMENT,
                source_id=session.causal_head_uuid,
                source="File attachment",
                attachment_kind="file_mention",
            ),
        ),
        purpose=RequestPurpose.AGENT,
        causal_head=session.causal_head_uuid,
        step=1,
    )
    session.prepare_model_invocation(invocation)
    session.finish_model_invocation(invocation.request_id, "completed")
    session.append_assistant_message(
        AssistantMessage(
            (TextContent("answer"),),
            parent_uuid=session.causal_head_uuid,
            usage=TokenUsage(),
        )
    )

    history = runtime.current_session_view().history

    assert history == (
        HistoryText("user", "inspect notes"),
        HistoryContextGroup(
            1,
            (HistoryContextItem("File attachment", "file_mention", "secret notes"),),
        ),
        HistoryText("assistant", "answer", is_final_answer=True),
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
        content=(ToolResult("todo-1", "updated", ToolResultPresentation("updated")),),
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
    binding = runtime.runtime.foreground
    cache = runtime.runtime.context_cache
    active = runtime.runtime.session
    anchor = HumanMessage(content="keep this session")
    active.append_human_message(anchor)
    active.append_attachment(TodoReminderAttachment("keep"))
    empty_id = "33333333-3333-3333-3333-333333333333"
    SessionStore(runtime.settings.paths.project_state_dir, empty_id).load()

    with pytest.raises(ValueError, match="contains no messages"):
        await runtime.resume_session(empty_id)

    assert runtime.runtime.session is active
    assert runtime.runtime.foreground is binding
    assert runtime.runtime.context_cache is cache
    assert runtime.status().session_id == _CURRENT_SESSION_ID
    assert any(
        isinstance(message, AttachmentMessage)
        for message in runtime.runtime.session.context_entries
    )


@pytest.mark.asyncio
async def test_cancelled_resume_keeps_the_complete_active_session_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _bootstrap_runtime(tmp_path)
    binding = runtime.runtime.foreground

    async def cancel(*_args: object, **_kwargs: object) -> object:
        raise asyncio.CancelledError

    monkeypatch.setattr(runtime.sessions, "restore", cancel)

    with pytest.raises(asyncio.CancelledError):
        await runtime.resume_session(_TARGET_SESSION_ID)

    assert runtime.runtime.foreground is binding
    assert runtime.runtime.session.session_id == _CURRENT_SESSION_ID


@pytest.mark.asyncio
async def test_stream_prevents_session_switch_until_invocation_finishes(
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
            context_runtime: SessionContextCache,
            turn_input: AgentTurnInput,
        ) -> AsyncIterator[AgentEvent]:
            del session, context_runtime, turn_input
            started.set()
            await release.wait()
            yield AgentInvocationSucceeded("done", 1, TokenUsage())

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


def test_complete_transcript_view_projects_persisted_content_only(
    tmp_path: Path,
) -> None:
    runtime = _bootstrap_runtime(tmp_path)
    session = runtime.runtime.session
    user = HumanMessage("show everything")
    session.append_human_message(user)
    assistant = AssistantMessage(
        (
            ReasoningContent("reason-1", ReasoningPresentation("redacted", ())),
            TextContent("answer"),
            ToolCall("call-1", "Read", {"path": "a.py", "lines": [1, 2]}),
        ),
        TokenUsage(),
        parent_uuid=user.uuid,
    )
    session.append_assistant_message(assistant)
    session.append_tool_results(
        (
            ToolResult(
                "call-1",
                "literal **untrusted** output",
                ToolResultPresentation("read it"),
            ),
        ),
        assistant,
    )
    session.append_attachment(FileMentionAttachment("a.py", "print('x')"))
    session.append_attachment(TodoReminderAttachment("transient reminder"))
    parent = session.causal_head_uuid
    assert parent is not None
    summary = ConversationSummaryMessage("durable summary", parent_uuid=parent)
    session.commit_compaction(
        (), summary, CompactBoundary(parent, summary.uuid, "manual", 10, "estimated")
    )

    view = runtime.current_transcript_view()

    assert any(isinstance(entry, TranscriptText) for entry in view.entries)
    reasoning = next(
        entry for entry in view.entries if isinstance(entry, TranscriptReasoning)
    )
    assert reasoning.presentation.parts == ()
    call = next(
        entry for entry in view.entries if isinstance(entry, TranscriptToolCall)
    )
    assert tuple(field.key for field in call.input.fields) == ("path", "lines")
    result = next(
        entry for entry in view.entries if isinstance(entry, TranscriptToolResult)
    )
    assert result.content == "literal **untrusted** output"
    assert any(isinstance(entry, TranscriptSummary) for entry in view.entries)
    attachments = tuple(
        entry for entry in view.entries if isinstance(entry, TranscriptAttachment)
    )
    assert tuple(entry.attachment_kind for entry in attachments) == ("file_mention",)
    assert "call-1" not in repr(view)

    session.append_human_message(HumanMessage("new", parent_uuid=summary.uuid))
    assert runtime.current_transcript_view().revision != view.revision
