"""Stateful user-level application façade."""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace

from my_code.agent.runner import InteractiveAgentRunner
from my_code.application.activity.monitor import ActivityMonitor
from my_code.application.activity.projection import ActivityProjection
from my_code.application.configuration.modes import ModeOperations
from my_code.application.configuration.providers import ProviderOperations
from my_code.application.contracts.events import (
    BackgroundInvocationFinished,
    BackgroundInvocationStarted,
    CompactionCompleted,
    CompactionStarted,
    InvocationOutcome,
    TurnEvent,
)
from my_code.application.contracts.history import (
    ResumedSession,
)
from my_code.application.contracts.inputs import PathSuggestion, QueuedInputView
from my_code.application.contracts.permissions import (
    PermissionHandler,
    PermissionModeSwitch,
    PermissionModeView,
)
from my_code.application.contracts.questions import QuestionHandler
from my_code.application.contracts.status import ApplicationStatus, ContextUsageView
from my_code.application.contracts.views import (
    BackgroundTaskView,
    CapabilitiesView,
    SessionUsageView,
    SessionView,
    SubagentTaskView,
    TranscriptView,
)
from my_code.application.runtime_views import (
    project_capabilities,
    project_context_status,
    project_runtime_status,
    project_session_usage,
)
from my_code.application.sessions.history_projection import project_history
from my_code.application.sessions.operations import SessionOperations
from my_code.application.sessions.transcript_projection import project_transcript
from my_code.application.turns.coordinator import TurnCoordinator
from my_code.application.turns.mentions.suggestions import WorkspacePathSuggester
from my_code.config.paths import SettingsScope
from my_code.config.settings import AgentSettings
from my_code.config.store import SettingsStore
from my_code.context.engine import ContextEngine
from my_code.conversation.models import (
    AssistantMessage,
    TextContent,
)
from my_code.conversation.proposed_plan import (
    extract_proposed_plan,
)
from my_code.features.background_tasks.notifications import (
    BackgroundTaskNotificationSource,
)
from my_code.features.background_tasks.wake import BackgroundTaskWakeSignal
from my_code.model.display import DisplayDensity
from my_code.permissions.models import PermissionMode
from my_code.providers.discovery import resolve_without_network
from my_code.providers.manager import (
    ModelView,
    ProviderManager,
    ProviderProbeRequest,
    ProviderProbeResult,
    ProviderUpdate,
    ProviderView,
)
from my_code.runtime.application import ApplicationRuntime
from my_code.sessions.catalog import SessionSummary
from my_code.sessions.models import CollaborationMode
from my_code.tools.executor import ToolExecutor


class ApplicationService:
    """Coordinate application use cases over the explicit ApplicationRuntime graph."""

    def __init__(
        self,
        *,
        context: ContextEngine,
        tool_executor: ToolExecutor,
        settings: AgentSettings,
        runtime: ApplicationRuntime,
        turns: TurnCoordinator,
        sessions: SessionOperations,
        provider_operations: ProviderOperations,
        mode_operations: ModeOperations,
        activity_projection: ActivityProjection,
        activity_monitor: ActivityMonitor,
        path_suggester: WorkspacePathSuggester | None = None,
        background_notifications: BackgroundTaskNotificationSource | None = None,
        background_wake_signal: BackgroundTaskWakeSignal | None = None,
    ) -> None:
        self.context = context
        self.tool_executor = tool_executor
        self.settings = settings
        self.runtime = runtime
        self._project_state_dir = settings.paths.project_state_dir
        self.path_suggester = path_suggester or WorkspacePathSuggester(settings.cwd)
        self.background_notifications = background_notifications
        self.background_wake_signal = background_wake_signal
        self._initialization_lock = asyncio.Lock()
        self._initialized = False
        self.turns = turns
        self.sessions = sessions
        self.providers_ops = provider_operations
        self.modes = mode_operations
        self.activity = activity_projection
        self.activity_monitor = activity_monitor

    @property
    def agent(self) -> InteractiveAgentRunner:
        return self.turns.agent

    @agent.setter
    def agent(self, agent: InteractiveAgentRunner) -> None:
        self.turns.replace_agent(agent)

    @property
    def provider_manager(self) -> ProviderManager:
        return self.providers_ops.manager

    @provider_manager.setter
    def provider_manager(self, manager: ProviderManager) -> None:
        self.providers_ops.replace_manager(manager)

    async def initialize(self) -> SessionView:
        """Refresh network-backed capabilities after the local UI is visible."""

        async with self._initialization_lock:
            if self._initialized:
                return self.current_session_view()
            connection = self.runtime.provider.router.connection
            await self.runtime.start()
            descriptor = connection.model_descriptor or resolve_without_network(
                connection.protocol,
                connection.base_url,
                connection.model,
                connection.limits,
            )
            async with self.runtime.operation_lock():
                environment = self.providers_ops.initialize_environment(
                    connection, descriptor
                )
                if environment is not None:
                    if not self.runtime.session.conversation:
                        start = self.runtime.session.start
                        self.runtime.session.configure_start(
                            replace(
                                start,
                                provider_id=connection.id,
                                model=connection.model,
                                model_limits=descriptor.limits,
                                model_limit_source=descriptor.source.value,
                                compact_trigger_tokens=(
                                    environment.compact_trigger_tokens
                                ),
                                provider_protocol=connection.protocol.value,
                            )
                        )
            self._initialized = True
            return self.current_session_view()

    def current_session_view(self) -> SessionView:
        session = self.runtime.session
        return SessionView(
            self.status(),
            project_history(
                session,
                catalog=self.runtime.tools.snapshot(),
                search_mode=self.settings.tool_search_mode,
                tool_executor=self.tool_executor,
            ),
        )

    def view_mode(self) -> DisplayDensity:
        return (
            SettingsStore(self.settings.paths)
            .load_scope(SettingsScope.USER)
            .tui_view_mode
            or DisplayDensity.CONCISE
        )

    def set_view_mode(self, mode: DisplayDensity) -> None:
        """Atomically persist the user-level main scrollback preference."""

        SettingsStore(self.settings.paths).set_user_tui_view_mode(mode)

    def current_transcript_view(self) -> TranscriptView:
        """Return the complete persisted conversation without storage internals."""

        return project_transcript(self.runtime.session)

    def subagent_transcript_view(self, task_id: str) -> TranscriptView:
        """Project one retained child Session through the same audit DTO."""

        return self.activity.subagent_transcript(task_id)

    def session_usage(self) -> SessionUsageView:
        return project_session_usage(self.runtime.session, self.context_status())

    def capabilities(self) -> CapabilitiesView:
        """Return a fresh catalog snapshot without leaking runtime objects."""

        return project_capabilities(
            tools=self.runtime.tools.snapshot(),
            skills=self.runtime.skills.catalog.snapshot(),
            mcp_servers=self.runtime.mcp.snapshots(),
            tool_search_mode=self.settings.tool_search_mode,
            session=self.runtime.session,
        )

    def background_tasks(self) -> tuple[BackgroundTaskView, ...]:
        return self.activity.background_tasks(self.runtime.session.session_id)

    def subagent_tasks(self) -> tuple[SubagentTaskView, ...]:
        return self.activity.subagent_tasks(self.runtime.session.session_id)

    async def stream_subagent_activity(
        self,
    ) -> AsyncIterator[tuple[SubagentTaskView, ...]]:
        async for view in self.activity_monitor.stream(self.subagent_tasks):
            yield view

    async def reload_skills(self) -> CapabilitiesView:
        async with self.runtime.operation_lock():
            await self.runtime.start()
            self.runtime.skills.reload()
            return self.capabilities()

    async def refresh_mcp(self, server: str) -> CapabilitiesView:
        async with self.runtime.operation_lock():
            await self.runtime.start()
            await self.runtime.mcp.refresh(server)
            return self.capabilities()

    async def reconnect_mcp(self, server: str) -> CapabilitiesView:
        async with self.runtime.operation_lock():
            await self.runtime.start()
            await self.runtime.mcp.reconnect(server)
            return self.capabilities()

    async def submit(self, prompt: str) -> InvocationOutcome:
        async with self.runtime.operation_lock():
            await self.runtime.start()
            return await self.turns.submit(
                self.runtime.session, self.runtime.context_cache, prompt
            )

    async def stream(self, prompt: str) -> AsyncIterator[TurnEvent]:
        async with self.runtime.operation_lock():
            await self.runtime.start()
            session = self.runtime.session
            async for event in self.turns.stream(
                session,
                self.runtime.context_cache,
                prompt,
                self.context_status,
            ):
                yield event

    def queue_input(self, prompt: str) -> QueuedInputView:
        """Start preparing a transient input without persisting it."""

        return self.turns.queue_input(prompt)

    def recall_latest_input(self) -> str | None:
        return self.turns.recall_latest_input()

    def queued_inputs(self) -> tuple[QueuedInputView, ...]:
        return self.turns.queued_inputs()

    async def stream_interactive(self) -> AsyncIterator[TurnEvent]:
        """Consume queued inputs across fresh step budgets until the queue is idle."""

        async with self.runtime.operation_lock():
            await self.runtime.start()
            async for event in self.turns.stream_interactive(
                self.runtime.session,
                self.runtime.context_cache,
                self.context_status,
            ):
                yield event

    def cancel_active_turn(self) -> None:
        self.turns.cancel_active_turn()

    async def stream_background_notifications(self) -> AsyncIterator[TurnEvent]:
        """Watch terminal background tasks and run idle continuations."""

        source = self.background_notifications
        signal = self.background_wake_signal
        if source is None or signal is None:
            return
        revision = signal.revision
        while True:
            async with self.runtime.operation_lock():
                await self.runtime.start()
                session = self.runtime.session
                if source.has_pending(session.session_id):
                    yield BackgroundInvocationStarted()
                    failed = False
                    try:
                        async for event in self.turns.stream_continuation(
                            session,
                            self.runtime.context_cache,
                            self.context_status,
                        ):
                            yield event
                    except asyncio.CancelledError:
                        raise
                    except Exception as error:
                        failed = True
                        yield BackgroundInvocationFinished(str(error))
                    else:
                        yield BackgroundInvocationFinished()
                    if not failed:
                        revision = signal.revision
                        continue
            revision = await signal.wait_for_change(revision)

    async def suggest_paths(self, query: str) -> tuple[PathSuggestion, ...]:
        return await self.path_suggester.suggest(query)

    def status(self) -> ApplicationStatus:
        session = self.runtime.session
        connection = self.runtime.provider.router.connection
        return project_runtime_status(
            settings=self.settings,
            session=session,
            connection=connection,
            permission_mode=self.runtime.permissions.policy.mode.value,
            execution_environment=self.runtime.permissions.execution_environment,
            capabilities=self.capabilities(),
        )

    def context_status(self) -> ContextUsageView:
        return project_context_status(
            self.context,
            self.runtime.session,
            self.runtime.context_cache,
            self.runtime.tools.snapshot(),
        )

    async def compact(self) -> ContextUsageView:
        completed: CompactionCompleted | None = None
        async for event in self.stream_compaction():
            if isinstance(event, CompactionCompleted):
                completed = event
        if completed is None:
            raise RuntimeError("Compaction stream ended without completion")
        return completed.status

    async def stream_compaction(self) -> AsyncIterator[TurnEvent]:
        """Run a manual full compaction with frontend-neutral lifecycle events."""

        async with self.runtime.operation_lock():
            session = self.runtime.session
            yield CompactionStarted("manual")
            tools = self.runtime.tools.snapshot()
            pre_compact_budget = self.context.inspect(
                session.context_planning_state(),
                self.runtime.context_cache,
                tools=tools.definitions,
            )
            outcome = await self.context.compact(
                session.context_planning_state(),
                "manual",
                recorder=session,
                pre_compact_budget=pre_compact_budget,
            )
            session.commit_compaction(
                outcome.replacements,
                outcome.summary,
                outcome.boundary,
            )
            yield CompactionCompleted("manual", outcome.usage, self.context_status())

    def set_permission_handler(self, handler: PermissionHandler) -> None:
        self.turns.set_permission_handler(handler)

    def set_question_handler(self, handler: QuestionHandler | None) -> None:
        self.turns.set_question_handler(handler)

    def current_collaboration_mode(self) -> CollaborationMode:
        return self.modes.collaboration_mode(self.runtime.session)

    def cycle_collaboration_mode(self) -> CollaborationMode:
        """Persist the target first, then publish its effective permission policy."""

        if (
            self.runtime.operation_lock().locked()
            or self.turns.is_active
            or self.turns.queued_inputs()
        ):
            raise RuntimeError("Collaboration mode can change only while input is idle")
        if self.turns.question_active:
            raise RuntimeError("Collaboration mode cannot change during Question")
        return self.modes.cycle_collaboration(
            self.runtime.session, self.runtime.permissions
        )

    def start_plan_implementation(self, *, fresh_context: bool) -> QueuedInputView:
        """Leave Plan mode and queue the canonical implementation instruction."""

        if self.current_collaboration_mode() is not CollaborationMode.PLAN:
            raise RuntimeError("No Plan-mode handoff is active")
        plan = _latest_proposed_plan(self.runtime.session.conversation)
        if not plan:
            raise RuntimeError("The session has no completed proposed plan")
        if fresh_context:
            previous = self.runtime.session
            session, policy = self.sessions.create_plan_handoff(
                previous,
                plan,
                permission_rules=self.runtime.permissions.policy.rules,
            )
            self.turns.rebind_session(session.session_id)
            self.runtime.publish_foreground(
                self.runtime.build_foreground(session, policy)
            )
        else:
            self.runtime.session.set_collaboration_mode(CollaborationMode.DEFAULT.value)
            self.runtime.permissions.restore_mode(
                PermissionMode(self.runtime.session.permission_mode)
            )
        return self.queue_input("Implement the approved plan.")

    def permission_modes(self) -> tuple[PermissionModeView, ...]:
        """Project process-local mode state without exposing the mutable policy."""

        return self.modes.permission_modes(
            self.runtime.session, self.runtime.permissions
        )

    def current_permission_mode(self) -> PermissionModeView:
        return self.modes.current_permission_mode(
            self.runtime.session, self.runtime.permissions
        )

    def cycle_permission_mode(self) -> PermissionModeSwitch:
        return self.modes.cycle_permission(
            self.runtime.session, self.runtime.permissions
        )

    def select_permission_mode(self, value: str) -> PermissionModeSwitch:
        return self.modes.select_permission(
            value, self.runtime.session, self.runtime.permissions
        )

    def confirm_full_access(self, allow: bool) -> PermissionModeView:
        return self.modes.confirm_full_access(
            allow, self.runtime.session, self.runtime.permissions
        )

    def providers(self) -> tuple[ProviderView, ...]:
        return self.providers_ops.providers()

    def models(self) -> tuple[ModelView, ...]:
        """Return the active provider's safe, local-only model catalog."""

        return self.providers_ops.models()

    async def refresh_provider_models(self, provider_id: str) -> ProviderView:
        async with self.runtime.operation_lock():
            return await self.providers_ops.refresh_models(provider_id)

    async def probe_provider(
        self, request: ProviderProbeRequest
    ) -> ProviderProbeResult:
        """Probe temporary connection details without mutating runtime or storage."""

        return await self.providers_ops.probe(request)

    async def select_provider(self, provider_id: str) -> ApplicationStatus:
        async with self.runtime.operation_lock():
            await self.providers_ops.select_provider(provider_id)
            return self.status()

    async def select_model(self, model_id: str) -> ApplicationStatus:
        """Persist and publish a local catalog selection as one operation."""

        async with self.runtime.operation_lock():
            await self.providers_ops.select_model(model_id)
            return self.status()

    async def configure_provider(
        self,
        update: ProviderUpdate,
        probe_result: ProviderProbeResult | None = None,
    ) -> ApplicationStatus:
        async with self.runtime.operation_lock():
            await self.providers_ops.configure(update, probe_result)
            return self.status()

    async def remove_provider_credential(self, provider_id: str) -> ApplicationStatus:
        """Remove a stored key and refresh the active connection when necessary."""

        async with self.runtime.operation_lock():
            await self.providers_ops.remove_credential(provider_id)
            return self.status()

    async def list_sessions(self) -> tuple[SessionSummary, ...]:
        return await self.sessions.list(self.runtime.session.session_id)

    async def resume_session(self, session_id: str) -> ResumedSession:
        async with self.runtime.operation_lock():
            if session_id == self.runtime.session.session_id:
                raise ValueError("Session is already active")
            if self.turns.queued_inputs():
                raise RuntimeError("Recall or clear queued inputs before resuming")
            candidate = await self.sessions.restore(
                session_id,
                permission_rules=self.runtime.permissions.policy.rules,
                tools=self.runtime.tools.snapshot(),
            )
            self.turns.rebind_session(candidate.session.session_id)
            self.runtime.publish_foreground(
                self.runtime.build_foreground(
                    candidate.session, candidate.permission_policy
                )
            )
            if self.background_wake_signal is not None:
                self.background_wake_signal.pulse()
            return ResumedSession(status=self.status(), history=candidate.history)

    async def close(self) -> None:
        await self.runtime.close()


def _latest_proposed_plan(
    conversation: tuple[object, ...],
) -> str | None:
    for entry in reversed(conversation):
        if not isinstance(entry, AssistantMessage):
            continue
        for block in reversed(entry.content):
            if isinstance(block, TextContent):
                plan = extract_proposed_plan(block.text)
                if plan:
                    return plan
    return None


__all__ = [
    "ApplicationService",
]
