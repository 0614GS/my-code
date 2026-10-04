"""Stateful user-level application façade."""

import asyncio
import re
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

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
from my_code.application.contracts.mcp import McpServerRegistration
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
    ExecutionArtifactsView,
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
from my_code.auth.mcp_bearer import McpBearerTokenStore
from my_code.auth.mcp_oauth import McpOAuthTokenStore
from my_code.config.paths import SettingsScope
from my_code.config.settings import AgentSettings
from my_code.config.store import McpServerSettingsLayer, SettingsLayer, SettingsStore
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
from my_code.mcp.models import (
    McpAuthKind,
    McpConnectionState,
    McpServerScope,
    McpServerSpec,
    McpServerTransport,
)
from my_code.model.display import DisplayDensity
from my_code.permissions.models import PermissionMode
from my_code.permissions.policy import PermissionPolicy
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
from my_code.sessions.models import CollaborationMode, SessionStart
from my_code.sessions.session import Session
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
        diagnostics_directory: Path | None = None,
        evaluation: dict[str, str | None] | None = None,
    ) -> None:
        self.context = context
        self.tool_executor = tool_executor
        self.settings = settings
        self.runtime = runtime
        self._project_state_dir = settings.paths.project_state_dir
        self.path_suggester = path_suggester or WorkspacePathSuggester(settings.cwd)
        self.background_notifications = background_notifications
        self.background_wake_signal = background_wake_signal
        self._diagnostics_directory = diagnostics_directory
        self.evaluation = evaluation
        self._initialization_lock = asyncio.Lock()
        self._initialized = False
        self.turns = turns
        self.sessions = sessions
        self.providers_ops = provider_operations
        self.modes = mode_operations
        self.activity = activity_projection
        self.activity_monitor = activity_monitor
        self._background_scheduler: asyncio.Task[None] | None = None
        self._background_events: asyncio.Queue[TurnEvent] = asyncio.Queue()

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
            self._ensure_background_scheduler()
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

    def execution_artifacts(self) -> ExecutionArtifactsView:
        """投影当前运行的持久化与诊断证据位置。"""

        paths = self.runtime.session.artifact_paths()
        return ExecutionArtifactsView(
            str(paths.session_log),
            str(paths.request_audit_log),
            (
                str(self._diagnostics_directory)
                if self._diagnostics_directory is not None
                else None
            ),
        )

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

    async def add_mcp(self, registration: McpServerRegistration) -> CapabilitiesView:
        """保存当前工作区的可信定义，并立即连接到当前 runtime。"""

        scope = (
            SettingsScope.USER
            if self.settings.paths.project_config_collides_with_user_storage
            else SettingsScope.LOCAL
        )
        name = registration.name
        if name is None:
            if registration.url is None:
                raise ValueError("MCP server name is required for stdio")
            host = urlsplit(registration.url).hostname or "mcp"
            base = re.sub(r"[^a-z0-9_-]+", "-", host.lower()).strip("-")[:55]
            base = base or "mcp"
            used = {item.name for item in self.runtime.mcp.snapshots()}
            name = base
            suffix = 2
            while name in used:
                name = f"{base[: 64 - len(str(suffix)) - 1]}-{suffix}"
                suffix += 1
        layer = McpServerSettingsLayer(
            name=name,
            command=registration.command,
            args=registration.args,
            env_from=registration.env_from,
            scope=scope,
            transport="http" if registration.url is not None else "stdio",
            url=registration.url,
            auth=(
                "auto"
                if registration.name is None and registration.url is not None
                else registration.auth
            ),
            bearer_token_from=registration.bearer_token_from,
        )
        spec = McpServerSpec(
            name=layer.name,
            command=layer.command,
            cwd=self.settings.cwd,
            args=layer.args,
            env_from=layer.env_from,
            scope=McpServerScope(scope.value),
            transport=McpServerTransport(layer.transport),
            url=layer.url,
            auth=McpAuthKind(layer.auth),
            bearer_token_from=layer.bearer_token_from,
        )
        async with self.runtime.operation_lock():
            await self.runtime.start()
            SettingsStore(self.settings.paths).write(
                scope,
                SettingsLayer(mcp_enabled=True, mcp_servers=(layer,)),
            )
            await self.runtime.mcp.add_or_replace(spec)
            return self.capabilities()

    async def authenticate_mcp(
        self, server_name: str, auth: McpAuthKind, *, token: str | None = None
    ) -> CapabilitiesView:
        """仅在连接成功后把鉴权方式写入设置。"""

        if auth not in {McpAuthKind.OAUTH, McpAuthKind.BEARER}:
            raise ValueError("MCP authentication must be OAuth or Bearer")
        previous = self.runtime.mcp.spec(server_name)
        if previous.auth is not McpAuthKind.AUTO:
            raise ValueError("MCP server is not awaiting automatic authentication")
        if previous.transport is not McpServerTransport.HTTP or previous.url is None:
            raise ValueError("Only HTTP MCP servers can authenticate")
        if previous.scope is McpServerScope.PROJECT:
            raise ValueError("Project MCP server must be trusted locally first")
        bearer_store = McpBearerTokenStore(
            self.settings.paths.config_home / ".mcp-bearer",
            server_name,
            previous.url,
        )
        old_token = bearer_store.load() if auth is McpAuthKind.BEARER else None
        if auth is McpAuthKind.BEARER:
            if token is None:
                raise ValueError("Bearer token is required")
            bearer_store.save(token)
        updated = replace(previous, auth=auth, bearer_token_from=None)
        async with self.runtime.operation_lock():
            try:
                snapshot = await self.runtime.mcp.add_or_replace(updated)
                if snapshot.state is not McpConnectionState.CONNECTED:
                    raise ValueError("MCP authentication did not connect")
                self._save_mcp_spec(updated)
            except BaseException:
                if auth is McpAuthKind.BEARER:
                    if old_token is None:
                        bearer_store.delete()
                    else:
                        bearer_store.save(old_token)
                await self.runtime.mcp.add_or_replace(previous)
                raise
            return self.capabilities()

    async def logout_mcp(self, server_name: str) -> CapabilitiesView:
        spec = self.runtime.mcp.spec(server_name)
        if spec.transport is not McpServerTransport.HTTP or spec.url is None:
            raise ValueError("Only HTTP MCP servers have remote credentials")
        if spec.bearer_token_from is not None:
            raise ValueError(
                "Environment-backed Bearer token must be removed from settings"
            )
        if spec.scope is McpServerScope.PROJECT:
            raise ValueError("Project MCP server must be trusted locally first")
        async with self.runtime.operation_lock():
            McpBearerTokenStore(
                self.settings.paths.config_home / ".mcp-bearer",
                server_name,
                spec.url,
            ).delete()
            McpOAuthTokenStore(
                self.settings.paths.config_home / ".mcp-oauth",
                server_name,
                spec.url,
            ).delete()
            anonymous = replace(spec, auth=McpAuthKind.AUTO)
            self._save_mcp_spec(anonymous)
            await self.runtime.mcp.add_or_replace(anonymous)
            return self.capabilities()

    def _save_mcp_spec(self, spec: McpServerSpec) -> None:
        scope = SettingsScope(spec.scope.value)
        SettingsStore(self.settings.paths).write(
            scope,
            SettingsLayer(
                mcp_servers=(
                    McpServerSettingsLayer(
                        name=spec.name,
                        command=spec.command,
                        args=spec.args,
                        env_from=spec.env_from,
                        scope=scope,
                        transport=spec.transport.value,
                        url=spec.url,
                        auth=spec.auth.value,
                        bearer_token_from=spec.bearer_token_from,
                        enabled=spec.enabled,
                        startup_timeout_seconds=spec.startup_timeout_seconds,
                        call_timeout_seconds=spec.call_timeout_seconds,
                    ),
                )
            ),
        )

    async def submit(self, prompt: str) -> InvocationOutcome:
        self._interrupt_background_waits()
        self._ensure_background_scheduler()
        async with self.runtime.operation_lock():
            await self.runtime.start()
            return await self.turns.submit(
                self.runtime.session, self.runtime.context_cache, prompt
            )

    async def stream(
        self,
        prompt: str,
        *,
        cancellation_message: str = "Tool execution was aborted by the user.",
    ) -> AsyncIterator[TurnEvent]:
        self._interrupt_background_waits()
        self._ensure_background_scheduler()
        async with self.runtime.operation_lock():
            await self.runtime.start()
            session = self.runtime.session
            async for event in self.turns.stream(
                session,
                self.runtime.context_cache,
                prompt,
                self.context_status,
                cancellation_message,
            ):
                yield event

    def queue_input(self, prompt: str) -> QueuedInputView:
        """Start preparing a transient input without persisting it."""

        queued = self.turns.queue_input(prompt)
        self._interrupt_background_waits(self.runtime.session.session_id)
        return queued

    def _interrupt_background_waits(self, owner: str | None = None) -> None:
        source = self.background_notifications
        if source is None:
            return
        if owner is None:
            source.registry.interrupt_all_waiters()
        else:
            source.registry.interrupt_waiters(owner)

    def recall_latest_input(self) -> str | None:
        return self.turns.recall_latest_input()

    def queued_inputs(self) -> tuple[QueuedInputView, ...]:
        return self.turns.queued_inputs()

    async def stream_interactive(self) -> AsyncIterator[TurnEvent]:
        """Consume queued inputs across fresh step budgets until the queue is idle."""

        self._ensure_background_scheduler()
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
        """只消费 Application 调度器发出的续跑事件。"""

        self._ensure_background_scheduler()
        while True:
            yield await self._background_events.get()

    def _ensure_background_scheduler(self) -> None:
        if self.background_notifications is None or self.background_wake_signal is None:
            return
        if self._background_scheduler is None or self._background_scheduler.done():
            self._background_scheduler = asyncio.create_task(
                self._run_background_scheduler(),
                name="my-code:background-continuations",
            )

    async def _run_background_scheduler(self) -> None:
        source = self.background_notifications
        signal = self.background_wake_signal
        assert source is not None and signal is not None
        revision = signal.revision
        while True:
            async with self.runtime.operation_lock():
                await self.runtime.start()
                session = self.runtime.session
                if not self.turns.queued_inputs() and source.has_watched_pending(
                    session.session_id
                ):
                    await self._background_events.put(BackgroundInvocationStarted())
                    failed = False
                    try:
                        async for event in self.turns.stream_continuation(
                            session, self.runtime.context_cache, self.context_status
                        ):
                            await self._background_events.put(event)
                    except asyncio.CancelledError:
                        raise
                    except Exception as error:
                        failed = True
                        await self._background_events.put(
                            BackgroundInvocationFinished(str(error))
                        )
                    else:
                        await self._background_events.put(
                            BackgroundInvocationFinished()
                        )
                    if not failed and source.has_watched_pending(session.session_id):
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
            source = session.compaction_input()
            outcome = await self.context.compact(
                source,
                "manual",
                recorder=session,
                pre_compact_budget=pre_compact_budget,
            )
            try:
                session.commit_compaction(
                    outcome.replacements,
                    outcome.summary,
                    outcome.boundary,
                    outcome.attachments,
                    source=source,
                )
            except BaseException:
                self.context.discard_compaction(outcome)
                raise
            await self.context.acknowledge_compaction(outcome)
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

    async def start_plan_implementation(
        self, *, fresh_context: bool
    ) -> QueuedInputView:
        """Leave Plan mode and queue the canonical implementation instruction."""

        async with self.runtime.operation_lock():
            if self.current_collaboration_mode() is not CollaborationMode.PLAN:
                raise RuntimeError("No Plan-mode handoff is active")
            plan = _latest_proposed_plan(self.runtime.session.conversation)
            if not plan:
                raise RuntimeError("The session has no completed proposed plan")
            if fresh_context:
                session, policy = self.sessions.create_fresh(
                    self._fresh_session_start(),
                    permission_rules=self.runtime.permissions.policy.rules,
                )
                self._publish_foreground(session, policy)
                return self.queue_input(_fresh_plan_prompt(plan))
            self.runtime.session.set_collaboration_mode(CollaborationMode.DEFAULT.value)
            self.runtime.permissions.restore_mode(
                PermissionMode(self.runtime.session.permission_mode)
            )
            return self.queue_input("Implement the approved plan.")

    async def new_session(self) -> SessionView:
        """创建并发布一个空的前台 Session。"""

        self._interrupt_background_waits()
        async with self.runtime.operation_lock():
            session, policy = self.sessions.create_fresh(
                self._fresh_session_start(),
                permission_rules=self.runtime.permissions.policy.rules,
            )
            self._publish_foreground(session, policy)
            return self.current_session_view()

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
        self._interrupt_background_waits()
        async with self.runtime.operation_lock():
            if session_id == self.runtime.session.session_id:
                raise ValueError("Session is already active")
            candidate = await self.sessions.restore(
                session_id,
                permission_rules=self.runtime.permissions.policy.rules,
                tools=self.runtime.tools.snapshot(),
            )
            self._publish_foreground(candidate.session, candidate.permission_policy)
            return ResumedSession(status=self.status(), history=candidate.history)

    def _fresh_session_start(self) -> SessionStart:
        connection = self.runtime.provider.router.connection
        environment = self.runtime.provider.environment()
        return SessionStart(
            session_id=str(uuid4()),
            created_at=datetime.now(UTC).isoformat(),
            cwd=str(self.runtime.workspace.root),
            provider_id=connection.id,
            model=connection.model,
            permission_mode=self.runtime.session.permission_mode,
            max_steps=self.settings.max_steps,
            max_output_tokens=self.settings.max_output_tokens,
            model_limits=environment.descriptor.limits,
            model_limit_source=environment.descriptor.source.value,
            compact_trigger_tokens=environment.compact_trigger_tokens,
            provider_protocol=connection.protocol.value,
            collaboration_mode=CollaborationMode.DEFAULT.value,
        )

    def _publish_foreground(self, session: Session, policy: PermissionPolicy) -> None:
        if self.turns.is_active:
            raise RuntimeError("Session can change only while input is idle")
        if self.turns.queued_inputs():
            raise RuntimeError("Recall or clear queued inputs before changing Session")
        self.turns.rebind_session(session.session_id)
        self.runtime.publish_foreground(self.runtime.build_foreground(session, policy))
        if self.background_wake_signal is not None:
            self.background_wake_signal.pulse()

    async def close(self) -> None:
        self._interrupt_background_waits()
        if self._background_scheduler is not None:
            self._background_scheduler.cancel()
            await asyncio.gather(self._background_scheduler, return_exceptions=True)
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


def _fresh_plan_prompt(plan: str) -> str:
    return (
        "A previous agent produced the plan below to accomplish the user's task. "
        "Implement the plan in a fresh context. Treat the plan as the source of "
        "user intent, re-read files as needed, and carry the work through "
        "implementation and verification.\n\n"
        f"{plan}"
    )


__all__ = [
    "ApplicationService",
]
