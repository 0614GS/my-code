"""Stateful user-level chat orchestration."""

import asyncio
import hashlib
from collections.abc import AsyncIterator, Callable
from dataclasses import fields, is_dataclass, replace
from datetime import UTC, datetime
from inspect import signature
from threading import Event as ThreadEvent
from threading import Thread
from typing import Protocol, cast
from uuid import uuid4

from my_code.agent.events import (
    AgentCompactionCompleted,
    AgentCompactionStarted,
    AgentConversationUpdated,
    AgentEvent,
    AgentInputAccepted,
    AgentInputFailed,
    AgentModelRequestPrepared,
    AgentModelStepCompleted,
    AgentPlanCompleted,
    AgentPlanDelta,
    AgentPlanStarted,
    AgentReasoningCompleted,
    AgentReasoningDelta,
    AgentReasoningStarted,
    AgentTextCompleted,
    AgentTextDelta,
    AgentTextStarted,
    AgentToolFinished,
    AgentToolStarted,
)
from my_code.agent.models import (
    AgentMaxStepsReached,
    AgentTurnInput,
    AgentTurnSucceeded,
)
from my_code.agent.runner import AgentRunner, InteractiveAgentRunner
from my_code.chat.events import (
    AttachmentLoaded,
    BackgroundInvocationFinished,
    BackgroundInvocationStarted,
    CompactionCompleted,
    CompactionStarted,
    ContextUpdated,
    MaxStepsReached,
    ModelRequestPrepared,
    ModelStepCompleted,
    PlanCompleted,
    PlanDelta,
    PlanStarted,
    PreparedContext,
    ReasoningCompleted,
    ReasoningDelta,
    ReasoningStarted,
    TextCompleted,
    TextDelta,
    TextStarted,
    TodoListUpdated,
    ToolFinished,
    ToolStarted,
    TurnEvent,
    TurnInputAccepted,
    TurnInputFailed,
    TurnOutcome,
    TurnSucceeded,
)
from my_code.chat.history import (
    HistoryContextGroup,
    HistoryContextItem,
    HistoryEntry,
    HistoryPlan,
    HistoryReasoning,
    HistoryText,
    HistoryToolCall,
    ResumedSession,
)
from my_code.chat.pending_inputs import PendingInputController, QueuedInputView
from my_code.chat.permissions import (
    DeferredPermissionPrompter,
    PermissionHandler,
    PermissionModeSwitch,
    PermissionModeView,
    permission_mode_view,
)
from my_code.chat.questions import DeferredQuestionBroker, QuestionHandler
from my_code.chat.status import ContextStatus, RuntimeStatus
from my_code.chat.views import (
    BackgroundTaskView,
    CapabilitiesView,
    CapabilityDiagnosticView,
    McpServerView,
    SessionUsageView,
    SessionView,
    SkillCapabilityView,
    SubagentTaskView,
    ToolCapabilityView,
    TranscriptAttachment,
    TranscriptEntry,
    TranscriptField,
    TranscriptReasoning,
    TranscriptSummary,
    TranscriptText,
    TranscriptToolCall,
    TranscriptToolResult,
    TranscriptValue,
    TranscriptView,
)
from my_code.config.paths import SettingsScope
from my_code.config.settings import AgentSettings
from my_code.config.store import SettingsStore
from my_code.context.engine import ContextEngine
from my_code.conversation.attachments import (
    AttachmentPayload,
    PlanHandoffAttachment,
    is_durable_attachment,
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
from my_code.conversation.presentation import ToolResultPresentation
from my_code.conversation.proposed_plan import (
    extract_proposed_plan,
    strip_proposed_plan,
)
from my_code.features.background_tasks.registry import BackgroundTaskRegistry
from my_code.features.file_mentions.loader import AttachmentLoader
from my_code.features.file_mentions.models import PathSuggestion
from my_code.features.file_mentions.suggestions import WorkspacePathSuggester
from my_code.features.todos.codec import TODO_WRITE_TOOL_NAME, parse_todo_input
from my_code.features.todos.projection import project_todos
from my_code.model.capabilities import (
    CapabilitySource,
    ModelDescriptor,
    resolve_environment,
)
from my_code.model.display import DisplayDensity
from my_code.model.invocation import ModelInputOriginKind
from my_code.model.primitives import ReasoningPresentation
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
from my_code.providers.router import ProviderConnection
from my_code.runtime.state import AppState
from my_code.sessions.catalog import SessionCatalog, SessionSummary
from my_code.sessions.models import CollaborationMode
from my_code.sessions.session import Session
from my_code.skills.tool import restore_skill_permissions
from my_code.tasks.models import (
    SubagentTaskView as RuntimeSubagentTaskView,
)
from my_code.tasks.models import (
    SubagentTranscriptReasoning,
    SubagentTranscriptText,
    SubagentTranscriptTool,
)
from my_code.tools.discovery import (
    ToolExposureSnapshot,
    restored_discoveries,
    unwrap_searched_tool_call,
)
from my_code.tools.executor import ToolExecutor
from my_code.tools.presentation import ToolUsePresentation, tool_display_category


class BackgroundNotificationSource(Protocol):
    def has_pending(self, owner_run_id: str) -> bool: ...


class BackgroundWakeSignal(Protocol):
    @property
    def revision(self) -> int: ...

    async def wait_for_change(self, after_revision: int) -> int: ...

    def pulse(self) -> None: ...


class SubagentActivitySource(Protocol):
    @property
    def activity_revision(self) -> int: ...

    def task_views(self, owner_run_id: str) -> tuple[RuntimeSubagentTaskView, ...]: ...

    async def wait_for_activity(self, after_revision: int) -> int: ...

    def session_for_task(self, task_id: str) -> Session | None: ...


def _connection_identity(
    connection: ProviderConnection,
) -> tuple[str, object, str, str | None]:
    return (
        connection.id,
        connection.protocol,
        connection.model,
        connection.base_url,
    )


def _accepts_pending_source(agent: object) -> bool:
    method = getattr(agent, "stream_continuation", None)
    return method is not None and "pending_source" in signature(method).parameters


async def _offload_session_io[T](operation: Callable[[], T]) -> T:
    """Run filesystem-heavy hydration without blocking the UI event loop.

    A small explicit worker is used instead of the process-global asyncio
    executor so Session restore cannot contend with provider or renderer work.
    """

    done = ThreadEvent()
    result: list[T] = []
    errors: list[BaseException] = []

    def run() -> None:
        try:
            result.append(operation())
        except BaseException as error:
            errors.append(error)
        finally:
            done.set()

    worker = Thread(target=run, name="my-code-session-io", daemon=True)
    worker.start()
    while not done.is_set():  # noqa: ASYNC110 - threading.Event is cross-thread
        await asyncio.sleep(0.001)
    worker.join()
    if errors:
        raise errors[0]
    return result[0]


def _project_subagent_entry(
    entry: SubagentTranscriptText
    | SubagentTranscriptReasoning
    | SubagentTranscriptTool,
) -> HistoryEntry:
    if isinstance(entry, SubagentTranscriptText):
        role = "user" if entry.role == "user" else "assistant"
        return HistoryText(role, entry.text, entry.streaming)
    if isinstance(entry, SubagentTranscriptReasoning):
        return HistoryReasoning(
            ReasoningPresentation(entry.disclosure, entry.parts), entry.streaming
        )
    use = ToolUsePresentation(
        entry.use.display_name,
        entry.use.summary,
        entry.use.activity,
        tool_display_category(entry.use.display_name),
    )
    result = (
        ToolResultPresentation(
            entry.result.summary, entry.result.detail, entry.result.truncated
        )
        if entry.result is not None
        else ToolResultPresentation("Tool is still running.")
    )
    return HistoryToolCall(
        entry.tool_use_id,
        use,
        result,
        entry.is_error,
        running=entry.result is None,
        name=entry.use.display_name,
    )


class ChatService:
    """Coordinate chat use cases over the explicit AppState graph."""

    def __init__(
        self,
        *,
        agent: AgentRunner,
        context: ContextEngine,
        tool_executor: ToolExecutor,
        settings: AgentSettings,
        permission_prompter: DeferredPermissionPrompter,
        question_broker: DeferredQuestionBroker | None = None,
        provider_manager: ProviderManager,
        state: AppState,
        attachment_loader: AttachmentLoader | None = None,
        path_suggester: WorkspacePathSuggester | None = None,
        background_notifications: BackgroundNotificationSource | None = None,
        background_wake_signal: BackgroundWakeSignal | None = None,
        background_tasks: BackgroundTaskRegistry | None = None,
        subagents: SubagentActivitySource | None = None,
        shutdown_observability: Callable[[], None] | None = None,
    ) -> None:
        self.agent = agent
        self.context = context
        self.tool_executor = tool_executor
        self.settings = settings
        self.permission_prompter = permission_prompter
        self.question_broker = question_broker or DeferredQuestionBroker()
        self.provider_manager = provider_manager
        self.state = state
        self._project_state_dir = settings.paths.project_state_dir
        self.attachment_loader = attachment_loader
        self.path_suggester = path_suggester or WorkspacePathSuggester(settings.cwd)
        self.background_notifications = background_notifications
        self.background_wake_signal = background_wake_signal
        self._background_tasks = background_tasks
        self._subagents = subagents
        self._initialization_lock = asyncio.Lock()
        self._initialized = False
        self._shutdown_observability = shutdown_observability or (lambda: None)
        self._pending_inputs = PendingInputController(
            state.session.session_id, attachment_loader
        )
        self._interactive_task: asyncio.Task[object] | None = None

    async def initialize(self) -> SessionView:
        """Refresh network-backed capabilities after the local UI is visible."""

        async with self._initialization_lock:
            if self._initialized:
                return self.current_session_view()
            connection = self.state.provider.router.connection
            await self.state.start()
            descriptor = connection.model_descriptor or resolve_without_network(
                connection.protocol,
                connection.base_url,
                connection.model,
                connection.limits,
            )
            async with self.state.operation_lock():
                current = self.state.provider.router.connection
                if _connection_identity(current) == _connection_identity(connection):
                    environment = resolve_environment(
                        descriptor,
                        requested_output_tokens=self.settings.max_output_tokens,
                        configured_trigger_tokens=(
                            connection.compact.trigger_input_tokens
                        ),
                        discovered_at=descriptor.discovered_at,
                    )
                    if connection.warning is not None:
                        environment = replace(environment, warning=connection.warning)
                    self.state.provider.update_environment(environment)
                    if not self.state.session.conversation:
                        start = self.state.session.start
                        self.state.session.configure_start(
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
        session = self.state.session
        return SessionView(self.status(), self._project_history(session))

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

        return self._transcript_view(self.state.session)

    def subagent_transcript_view(self, task_id: str) -> TranscriptView:
        """Project one retained child Session through the same audit DTO."""

        source = self._subagents
        getter = getattr(source, "session_for_task", None)
        session = getter(task_id) if getter is not None else None
        if session is None:
            raise ValueError(f"Subagent transcript is unavailable: {task_id}")
        return self._transcript_view(session)

    def _transcript_view(self, session: Session) -> TranscriptView:
        """Build one complete conversation and request-audit snapshot."""

        conversation = session.conversation
        tool_names = {
            block.id: block.name
            for message in conversation
            if isinstance(message, AssistantMessage)
            for block in message.content
            if isinstance(block, ToolCall)
        }
        entries: list[TranscriptEntry] = []
        for message in conversation:
            if isinstance(message, HumanMessage):
                entries.append(TranscriptText("user", message.content))
            elif isinstance(message, AssistantMessage):
                has_tools = any(
                    isinstance(block, ToolCall) for block in message.content
                )
                for block in message.content:
                    if isinstance(block, TextContent):
                        entries.append(
                            TranscriptText(
                                "assistant",
                                block.text,
                                is_final_answer=not has_tools,
                            )
                        )
                    elif isinstance(block, ReasoningContent):
                        presentation = block.presentation
                        if presentation.disclosure in {"hidden", "redacted"}:
                            presentation = ReasoningPresentation(
                                presentation.disclosure, ()
                            )
                        entries.append(TranscriptReasoning(presentation))
                    else:
                        entries.append(
                            TranscriptToolCall(
                                block.name, _transcript_value(block.input)
                            )
                        )
            elif isinstance(message, ToolResultBatch):
                entries.extend(
                    TranscriptToolResult(
                        tool_names.get(result.tool_use_id, "Tool"),
                        result.content,
                        result.is_error,
                    )
                    for result in message.content
                )
            elif isinstance(message, ConversationSummaryMessage):
                entries.append(TranscriptSummary(message.content))
            elif isinstance(message, AttachmentMessage) and is_durable_attachment(
                message.payload
            ):
                entries.append(
                    TranscriptAttachment(
                        message.payload.kind,
                        _transcript_value(message.payload, omitted={"owner_run_id"}),
                    )
                )
        audit = session.request_audit_snapshot()
        digest = hashlib.sha256(
            "\0".join(message.uuid for message in conversation).encode()
            + audit.revision.to_bytes(8, "big")
        ).digest()
        revision = int.from_bytes(digest[:8], "big")
        return TranscriptView(
            revision,
            tuple(entries),
            audit.requests,
            audit.legacy_missing,
        )

    def session_usage(self) -> SessionUsageView:
        usages = (
            message.usage
            for message in self.state.session.conversation
            if isinstance(message, AssistantMessage)
        )
        request_count = 0
        input_tokens = 0
        output_tokens = 0
        cache_creation = 0
        cache_read = 0
        for usage in usages:
            request_count += 1
            input_tokens += usage.input_tokens
            output_tokens += usage.output_tokens
            cache_creation += usage.cache_creation_input_tokens
            cache_read += usage.cache_read_input_tokens
        return SessionUsageView(
            request_count,
            input_tokens,
            cache_creation,
            cache_read,
            output_tokens,
            self.context_status(),
        )

    def capabilities(self) -> CapabilitiesView:
        """Return a fresh catalog snapshot without leaking runtime objects."""

        tools = self.state.tools.snapshot()
        exposure = ToolExposureSnapshot.build(
            tools,
            self.settings.tool_search_mode,
            restored_discoveries(self.state.session.conversation),
        )
        skill_snapshot = self.state.skills.catalog.snapshot()
        return CapabilitiesView(
            tools=tuple(
                ToolCapabilityView(
                    registration.tool.definition.name,
                    registration.tool.definition.description,
                    str(registration.source),
                    (
                        "searched"
                        if registration.tool.definition.name in exposure.searched
                        else registration.tool.exposure.value
                    ),
                    (
                        "direct"
                        if registration.tool.definition.name in exposure.direct_tools
                        else "via InvokeSearchedTool"
                        if registration.tool.definition.name in exposure.searched
                        else "via ToolSearch"
                    ),
                )
                for registration in tools.registrations
            ),
            skills=tuple(
                SkillCapabilityView(
                    entry.name,
                    entry.description,
                    str(entry.source),
                    entry.compatibility,
                )
                for entry in skill_snapshot.entries
            ),
            skill_diagnostics=tuple(
                CapabilityDiagnosticView(
                    str(diagnostic.source),
                    diagnostic.code.value,
                    diagnostic.message,
                )
                for diagnostic in skill_snapshot.diagnostics
            ),
            mcp_servers=tuple(
                McpServerView(
                    server.name,
                    server.state.value,
                    server.tool_names,
                    (
                        CapabilityDiagnosticView(
                            server.name,
                            server.diagnostic.code.value,
                            server.diagnostic.message,
                        )
                        if server.diagnostic is not None
                        else None
                    ),
                )
                for server in self.state.mcp.snapshots()
            ),
            tool_search_mode=self.settings.tool_search_mode.value,
        )

    def background_tasks(self) -> tuple[BackgroundTaskView, ...]:
        registry = self._background_tasks
        if registry is None and self._subagents is None:
            return ()
        owner = self.state.session.session_id
        views: list[BackgroundTaskView] = []
        for item in () if registry is None else registry.tasks_for(owner):
            if item.task_type == "subagent" and self._subagents is not None:
                continue
            assert registry is not None
            snapshot = registry.tasks.snapshot(item.task_id)
            output_path = item.details.get("output_file")
            views.append(
                BackgroundTaskView(
                    task_id=item.task_id,
                    task_type=item.task_type,
                    summary=item.summary,
                    status=snapshot.status.value,
                    created_at=snapshot.created_at,
                    started_at=snapshot.started_at,
                    finished_at=snapshot.finished_at,
                    output_path=(output_path if isinstance(output_path, str) else None),
                    error=(
                        snapshot.failure.message
                        if snapshot.failure is not None
                        else None
                    ),
                )
            )
        if self._subagents is not None:
            views.extend(
                BackgroundTaskView(
                    task_id=item.task_id,
                    task_type=(
                        "subagent/background"
                        if item.background
                        else "subagent/foreground"
                    ),
                    summary=item.description,
                    status=item.status,
                    created_at=item.created_at,
                    started_at=item.started_at,
                    finished_at=item.finished_at,
                    output_path=None,
                    error=item.error,
                )
                for item in self.subagent_tasks()
            )
        return tuple(views)

    def subagent_tasks(self) -> tuple[SubagentTaskView, ...]:
        if self._subagents is None:
            return ()
        return tuple(
            SubagentTaskView(
                task_id=item.task_id,
                run_id=item.run_id,
                agent_type=item.agent_type,
                description=item.description,
                background=item.background,
                status=item.status,
                created_at=item.created_at,
                started_at=item.started_at,
                finished_at=item.finished_at,
                input_tokens=item.input_tokens,
                output_tokens=item.output_tokens,
                transcript=tuple(
                    _project_subagent_entry(entry) for entry in item.transcript
                ),
                active_tool_ids=item.active_tool_ids,
                error=item.error,
            )
            for item in self._subagents.task_views(self.state.session.session_id)
        )

    async def stream_subagent_activity(
        self,
    ) -> AsyncIterator[tuple[SubagentTaskView, ...]]:
        if self._subagents is None:
            return
        revision = -1
        while True:
            current = self._subagents.activity_revision
            if current != revision:
                revision = current
                yield self.subagent_tasks()
            try:
                revision = await asyncio.wait_for(
                    self._subagents.wait_for_activity(revision), timeout=1.0
                )
            except TimeoutError:
                if any(
                    item.status in {"pending", "running", "cancelling"}
                    for item in self.subagent_tasks()
                ):
                    yield self.subagent_tasks()

    async def reload_skills(self) -> CapabilitiesView:
        async with self.state.operation_lock():
            await self.state.start()
            self.state.skills.reload()
            return self.capabilities()

    async def refresh_mcp(self, server: str) -> CapabilitiesView:
        async with self.state.operation_lock():
            await self.state.start()
            await self.state.mcp.refresh(server)
            return self.capabilities()

    async def reconnect_mcp(self, server: str) -> CapabilitiesView:
        async with self.state.operation_lock():
            await self.state.start()
            await self.state.mcp.reconnect(server)
            return self.capabilities()

    async def submit(self, prompt: str) -> TurnOutcome:
        async with self.state.operation_lock():
            await self.state.start()
            attachments = await self._load_attachments(prompt)
            result = await self.agent.submit(
                self.state.session,
                self.state.context_runtime,
                AgentTurnInput(prompt, attachments),
            )
        return _project_turn_outcome(result)

    async def stream(self, prompt: str) -> AsyncIterator[TurnEvent]:
        async with self.state.operation_lock():
            await self.state.start()
            loaded = (
                await self.attachment_loader.load(prompt)
                if self.attachment_loader is not None
                else ()
            )
            for item in loaded:
                yield AttachmentLoaded(item.path, item.is_directory, item.display)
            session = self.state.session
            events = self.agent.stream(
                session,
                self.state.context_runtime,
                AgentTurnInput(prompt, tuple(item.attachment for item in loaded)),
            )
            try:
                async for event in self._project_agent_events(session, events):
                    yield event
            except asyncio.CancelledError:
                session.close_unresolved_tool_calls(
                    "Tool execution was aborted by the user."
                )
                raise

    def queue_input(self, prompt: str) -> QueuedInputView:
        """Start preparing a transient input without persisting it."""

        if self._pending_inputs.session_id != self.state.session.session_id:
            raise RuntimeError("Pending input controller is bound to another session")
        return self._pending_inputs.queue_input(prompt)

    def recall_latest_input(self) -> str | None:
        return self._pending_inputs.recall_latest_input()

    def queued_inputs(self) -> tuple[QueuedInputView, ...]:
        return self._pending_inputs.queued_inputs()

    async def stream_interactive(self) -> AsyncIterator[TurnEvent]:
        """Consume queued inputs across fresh step budgets until the queue is idle."""

        async with self.state.operation_lock():
            if not _accepts_pending_source(self.agent):
                raise RuntimeError("Agent runner does not support interactive steering")
            await self.state.start()
            self._interactive_task = asyncio.current_task()
            try:
                while self._pending_inputs.has_actionable():
                    await self._pending_inputs.prepare_pending()
                    for failure in self._pending_inputs.drain_failures():
                        yield TurnInputFailed(
                            failure.input_id,
                            failure.prompt,
                            failure.error or "Attachment preparation failed",
                        )
                    if not self._pending_inputs.has_actionable():
                        break
                    session = self.state.session
                    events = cast(
                        InteractiveAgentRunner, self.agent
                    ).stream_continuation(
                        session,
                        self.state.context_runtime,
                        pending_source=self._pending_inputs,
                    )
                    try:
                        async for event in self._project_agent_events(session, events):
                            yield event
                        for failure in self._pending_inputs.drain_failures():
                            yield TurnInputFailed(
                                failure.input_id,
                                failure.prompt,
                                failure.error or "Attachment preparation failed",
                            )
                    except asyncio.CancelledError:
                        session.close_unresolved_tool_calls(
                            "Tool execution was aborted by the user."
                        )
                        raise
            finally:
                self._interactive_task = None

    def cancel_active_turn(self) -> None:
        task = self._interactive_task
        if task is not None and not task.done():
            task.cancel()

    async def stream_background_notifications(self) -> AsyncIterator[TurnEvent]:
        """Watch terminal background tasks and run idle continuations."""

        source = self.background_notifications
        signal = self.background_wake_signal
        if source is None or signal is None:
            return
        revision = signal.revision
        while True:
            async with self.state.operation_lock():
                await self.state.start()
                session = self.state.session
                if source.has_pending(session.session_id):
                    yield BackgroundInvocationStarted()
                    failed = False
                    try:
                        events = (
                            cast(
                                InteractiveAgentRunner, self.agent
                            ).stream_continuation(
                                session,
                                self.state.context_runtime,
                                pending_source=self._pending_inputs,
                            )
                            if _accepts_pending_source(self.agent)
                            else self.agent.stream_continuation(
                                session, self.state.context_runtime
                            )
                        )
                        async for event in self._project_agent_events(session, events):
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

    async def _project_agent_events(
        self,
        session: Session,
        events: AsyncIterator[AgentEvent],
    ) -> AsyncIterator[TurnEvent]:
        previous_todo_write_id = project_todos(session.conversation).latest_write_id
        last_input_context_counts: tuple[int, int] | None = None
        async for event in events:
            if isinstance(event, AgentCompactionStarted):
                yield CompactionStarted(event.trigger)
            elif isinstance(event, AgentCompactionCompleted):
                yield CompactionCompleted(
                    event.trigger,
                    event.usage,
                    self.context_status(),
                )
            elif isinstance(event, AgentModelRequestPrepared):
                yield ModelRequestPrepared(
                    event.request_id,
                    event.request_number,
                    event.purpose,
                    tuple(
                        PreparedContext(
                            item.audit_id,
                            item.source,
                            item.attachment_kind,
                            item.text,
                        )
                        for item in event.injections
                    ),
                )
            elif isinstance(event, AgentInputAccepted):
                yield TurnInputAccepted(event.input_id, event.prompt)
                # The first context snapshot is intentionally created only
                # after a user input is canonical. Hosts can therefore avoid
                # presenting a speculative startup estimate. One persistence
                # batch emits multiple accepted events, so de-duplicate the
                # identical projection by its committed entry counts.
                counts = (session.context_entry_count, session.conversation_entry_count)
                if counts != last_input_context_counts:
                    last_input_context_counts = counts
                    yield ContextUpdated(self.context_status())
            elif isinstance(event, AgentInputFailed):
                yield TurnInputFailed(event.input_id, event.prompt, event.error)
            elif isinstance(event, AgentTextStarted):
                yield TextStarted()
            elif isinstance(event, AgentTextDelta):
                yield TextDelta(event.text)
            elif isinstance(event, AgentTextCompleted):
                yield TextCompleted(event.text)
            elif isinstance(event, AgentPlanStarted):
                yield PlanStarted()
            elif isinstance(event, AgentPlanDelta):
                yield PlanDelta(event.text)
            elif isinstance(event, AgentPlanCompleted):
                yield PlanCompleted(event.plan)
            elif isinstance(event, AgentReasoningStarted):
                yield ReasoningStarted(event.disclosure)
            elif isinstance(event, AgentReasoningDelta):
                yield ReasoningDelta(event.disclosure, event.part_index, event.text)
            elif isinstance(event, AgentReasoningCompleted):
                yield ReasoningCompleted(event.presentation)
            elif isinstance(event, AgentModelStepCompleted):
                yield ModelStepCompleted(event.step_index, event.has_tools)
            elif isinstance(event, AgentToolStarted):
                yield ToolStarted(
                    event.tool_use_id,
                    event.presentation,
                    event.name,
                    event.input,
                )
            elif isinstance(event, AgentToolFinished):
                yield ToolFinished(
                    event.tool_use_id, event.is_error, event.presentation
                )
            elif isinstance(event, AgentConversationUpdated):
                todo_projection = project_todos(session.conversation)
                if todo_projection.latest_write_id != previous_todo_write_id:
                    previous_todo_write_id = todo_projection.latest_write_id
                    if todo_projection.latest_write_todos is not None:
                        yield TodoListUpdated(todo_projection.latest_write_todos)
                yield ContextUpdated(self.context_status())
            elif isinstance(event, AgentTurnSucceeded):
                yield TurnSucceeded(
                    event.text,
                    event.completed_steps,
                    event.usage.input_tokens,
                    event.usage.output_tokens,
                )
            elif isinstance(event, AgentMaxStepsReached):
                yield MaxStepsReached(
                    event.max_steps,
                    event.completed_steps,
                    event.usage.input_tokens,
                    event.usage.output_tokens,
                )

    async def suggest_paths(self, query: str) -> tuple[PathSuggestion, ...]:
        return await self.path_suggester.suggest(query)

    def status(self) -> RuntimeStatus:
        session = self.state.session
        connection = self.state.provider.router.connection
        capabilities = self.capabilities()
        return RuntimeStatus(
            session_id=session.session_id,
            cwd=str(self.settings.cwd),
            provider_id=connection.id,
            base_url=connection.base_url,
            model=connection.model,
            permission_mode=self.state.permissions.policy.mode.value,
            credential_source=connection.credential_source.value,
            context_entry_count=session.context_entry_count,
            conversation_entry_count=session.conversation_entry_count,
            todos=project_todos(session.conversation).todos,
            tool_count=len(capabilities.tools),
            skill_count=len(capabilities.skills),
            mcp_connected_count=sum(
                server.state == "connected" for server in capabilities.mcp_servers
            ),
            mcp_server_count=len(capabilities.mcp_servers),
            execution_environment=self.state.permissions.execution_environment,
            collaboration_mode=session.collaboration_mode,
        )

    def context_status(self) -> ContextStatus:
        session = self.state.session
        tools = self.state.tools.snapshot()
        budget = self.context.inspect(
            session.context_planning_state(),
            self.state.context_runtime,
            tools=tools.definitions,
        )
        return ContextStatus(
            reported_base_tokens=budget.reported_base_tokens,
            estimated_delta_tokens=budget.estimated_delta_tokens,
            projected_tokens=budget.projected_tokens,
            reserved_output_tokens=budget.reserved_output_tokens,
            context_entry_count=session.context_entry_count,
            conversation_entry_count=session.conversation_entry_count,
            replacement_count=session.content_replacement_count,
            compact_count=session.compact_count,
            input_limit_tokens=budget.input_limit_tokens,
            compact_trigger_tokens=budget.compact_trigger_tokens,
            remaining_input_tokens=budget.remaining_input_tokens,
            measurement=budget.measurement,
            model_limit_source=budget.model_limit_source.value,
            configured_compact_trigger_tokens=budget.configured_compact_trigger_tokens,
            warning=budget.warning,
        )

    async def compact(self) -> ContextStatus:
        completed: CompactionCompleted | None = None
        async for event in self.stream_compaction():
            if isinstance(event, CompactionCompleted):
                completed = event
        if completed is None:
            raise RuntimeError("Compaction stream ended without completion")
        return completed.status

    async def stream_compaction(self) -> AsyncIterator[TurnEvent]:
        """Run a manual full compaction with frontend-neutral lifecycle events."""

        async with self.state.operation_lock():
            session = self.state.session
            yield CompactionStarted("manual")
            tools = self.state.tools.snapshot()
            pre_compact_budget = self.context.inspect(
                session.context_planning_state(),
                self.state.context_runtime,
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
        self.permission_prompter.set_handler(handler)

    def set_question_handler(self, handler: QuestionHandler | None) -> None:
        self.question_broker.set_handler(handler)

    def current_collaboration_mode(self) -> CollaborationMode:
        return CollaborationMode(self.state.session.collaboration_mode)

    def cycle_collaboration_mode(self) -> CollaborationMode:
        """Persist the target first, then publish its effective permission policy."""

        if (
            self.state.operation_lock().locked()
            or self._interactive_task is not None
            or self._pending_inputs.queued_inputs()
        ):
            raise RuntimeError("Collaboration mode can change only while input is idle")
        if self.question_broker.is_active:
            raise RuntimeError("Collaboration mode cannot change during Question")
        current = self.current_collaboration_mode()
        target = (
            CollaborationMode.PLAN
            if current is CollaborationMode.DEFAULT
            else CollaborationMode.DEFAULT
        )
        self.state.session.set_collaboration_mode(target.value)
        effective = (
            PermissionMode.PLAN
            if target is CollaborationMode.PLAN
            else PermissionMode(self.state.session.permission_mode)
        )
        self.state.permissions.restore_mode(effective)
        return target

    def start_plan_implementation(self, *, fresh_context: bool) -> QueuedInputView:
        """Leave Plan mode and queue the canonical implementation instruction."""

        if self.current_collaboration_mode() is not CollaborationMode.PLAN:
            raise RuntimeError("No Plan-mode handoff is active")
        plan = _latest_proposed_plan(self.state.session.conversation)
        if not plan:
            raise RuntimeError("The session has no completed proposed plan")
        if fresh_context:
            previous = self.state.session
            session_id = str(uuid4())
            start = replace(
                previous.start,
                session_id=session_id,
                created_at=datetime.now(UTC).isoformat(),
                permission_mode=previous.permission_mode,
                collaboration_mode=CollaborationMode.DEFAULT.value,
            )
            session = Session(
                self._project_state_dir,
                session_id,
                tool_results_dir=self.settings.paths.tool_results_dir(session_id),
                start=start,
            )
            session.append_attachment(PlanHandoffAttachment(plan))
            policy = PermissionPolicy(
                PermissionMode(previous.permission_mode),
                self.state.permissions.policy.rules,
            )
            self.state.replace_session(session, permission_policy=policy)
            self._pending_inputs = PendingInputController(
                session_id, self.attachment_loader
            )
        else:
            self.state.session.set_collaboration_mode(CollaborationMode.DEFAULT.value)
            self.state.permissions.restore_mode(
                PermissionMode(self.state.session.permission_mode)
            )
        return self.queue_input("Implement the approved plan.")

    def permission_modes(self) -> tuple[PermissionModeView, ...]:
        """Project process-local mode state without exposing the mutable policy."""

        if self.current_collaboration_mode() is CollaborationMode.PLAN:
            return ()
        state = self.state.permissions
        current = state.policy.mode
        return tuple(
            permission_mode_view(
                mode,
                current=mode is current,
                sandbox_active=state.sandbox_active,
                requires_confirmation=state.requires_full_access_confirmation(mode),
            )
            for mode in (
                PermissionMode.DEFAULT,
                PermissionMode.ACCEPT_EDITS,
                PermissionMode.BYPASS,
            )
        )

    def current_permission_mode(self) -> PermissionModeView:
        current = self.state.permissions.policy.mode
        if current is PermissionMode.PLAN:
            current = PermissionMode(self.state.session.permission_mode)
        return permission_mode_view(
            current,
            current=True,
            sandbox_active=self.state.permissions.sandbox_active,
            requires_confirmation=(self.state.permissions.full_access_pending),
        )

    def cycle_permission_mode(self) -> PermissionModeSwitch:
        if self.current_collaboration_mode() is CollaborationMode.PLAN:
            raise RuntimeError("Permissions cannot change in Plan mode")
        target, needs_confirmation = self.state.permissions.request_cycle(
            self._persist_permission_mode
        )
        view = permission_mode_view(
            target,
            current=not needs_confirmation,
            sandbox_active=self.state.permissions.sandbox_active,
            requires_confirmation=needs_confirmation,
        )
        return PermissionModeSwitch(view, not needs_confirmation, needs_confirmation)

    def select_permission_mode(self, value: str) -> PermissionModeSwitch:
        if self.current_collaboration_mode() is CollaborationMode.PLAN:
            raise RuntimeError("Permissions cannot change in Plan mode")
        try:
            requested = PermissionMode(value)
        except ValueError as error:
            raise ValueError(f"Unknown permission mode: {value}") from error
        current = self.state.permissions.policy.mode
        target, needs_confirmation = self.state.permissions.request_mode(
            requested, self._persist_permission_mode
        )
        view = permission_mode_view(
            target,
            current=not needs_confirmation,
            sandbox_active=self.state.permissions.sandbox_active,
            requires_confirmation=needs_confirmation,
        )
        return PermissionModeSwitch(
            view,
            target is not current and not needs_confirmation,
            needs_confirmation,
        )

    def confirm_full_access(self, allow: bool) -> PermissionModeView:
        mode = self.state.permissions.confirm_full_access(
            allow, self._persist_permission_mode
        )
        return permission_mode_view(
            mode,
            current=True,
            sandbox_active=self.state.permissions.sandbox_active,
            requires_confirmation=False,
        )

    def providers(self) -> tuple[ProviderView, ...]:
        return self.provider_manager.list(self.state.provider.router.connection.id)

    def models(self) -> tuple[ModelView, ...]:
        """Return the active provider's safe, local-only model catalog."""

        current = self.state.provider.router.connection.id
        view = next(
            item for item in self.provider_manager.list(current) if item.id == current
        )
        return view.model_catalog

    async def refresh_provider_models(self, provider_id: str) -> ProviderView:
        async with self.state.operation_lock():
            view = await self.provider_manager.refresh_models(provider_id)
            connection = self.state.provider.router.connection
            if connection.id == provider_id:
                refreshed = self.provider_manager.resolve(provider_id)
                descriptor = refreshed.model_descriptor or ModelDescriptor(
                    view.model,
                    view.model,
                    view.resolved_limits,
                    source=(
                        CapabilitySource(view.capability_source)
                        if view.capability_source is not None
                        else CapabilitySource.FALLBACK
                    ),
                )
                environment = resolve_environment(
                    descriptor,
                    requested_output_tokens=self.settings.max_output_tokens,
                    configured_trigger_tokens=refreshed.compact.trigger_input_tokens,
                    discovered_at=view.discovered_at,
                    discovery_error=view.discovery_error,
                )
                if refreshed.warning is not None:
                    environment = replace(environment, warning=refreshed.warning)
                if _connection_identity(connection) == _connection_identity(refreshed):
                    self.state.provider.update_environment(environment)
                else:
                    await self.state.provider.switch(refreshed, environment)
            return view

    async def probe_provider(
        self, request: ProviderProbeRequest
    ) -> ProviderProbeResult:
        """Probe temporary connection details without mutating runtime or storage."""

        return await self.provider_manager.probe(request)

    async def select_provider(self, provider_id: str) -> RuntimeStatus:
        async with self.state.operation_lock():
            connection = self.provider_manager.select_provider(provider_id)
            descriptor = resolve_without_network(
                connection.protocol,
                connection.base_url,
                connection.model,
                connection.limits,
            )
            environment = resolve_environment(
                descriptor,
                requested_output_tokens=self.settings.max_output_tokens,
                configured_trigger_tokens=connection.compact.trigger_input_tokens,
            )
            if connection.warning is not None:
                environment = replace(environment, warning=connection.warning)
            await self.state.provider.switch(connection, environment)
            return self.status()

    async def select_model(self, model_id: str) -> RuntimeStatus:
        """Persist and publish a local catalog selection as one operation."""

        async with self.state.operation_lock():
            current = self.state.provider.router.connection
            old_model = current.model
            connection = self.provider_manager.select_model(current.id, model_id)
            descriptor = connection.model_descriptor or resolve_without_network(
                connection.protocol,
                connection.base_url,
                connection.model,
                connection.limits,
            )
            environment = resolve_environment(
                descriptor,
                requested_output_tokens=self.settings.max_output_tokens,
                configured_trigger_tokens=connection.compact.trigger_input_tokens,
            )
            if connection.warning is not None:
                environment = replace(environment, warning=connection.warning)
            try:
                await self.state.provider.switch(connection, environment)
            except BaseException:
                self.provider_manager.select_model(current.id, old_model)
                raise
            return self.status()

    async def configure_provider(
        self,
        update: ProviderUpdate,
        probe_result: ProviderProbeResult | None = None,
    ) -> RuntimeStatus:
        async with self.state.operation_lock():
            connection = self.provider_manager.configure(
                update, probe_result=probe_result
            )
            descriptor = resolve_without_network(
                connection.protocol,
                connection.base_url,
                connection.model,
                connection.limits,
            )
            environment = resolve_environment(
                descriptor,
                requested_output_tokens=self.settings.max_output_tokens,
                configured_trigger_tokens=connection.compact.trigger_input_tokens,
            )
            if connection.warning is not None:
                environment = replace(environment, warning=connection.warning)
            await self.state.provider.switch(connection, environment)
            return self.status()

    async def remove_provider_credential(self, provider_id: str) -> RuntimeStatus:
        """Remove a stored key and refresh the active connection when necessary."""

        async with self.state.operation_lock():
            removed = self.provider_manager.delete_credential(provider_id)
            current = self.state.provider.router.connection
            if not removed or current.id != provider_id:
                return self.status()
            connection = self.provider_manager.resolve(provider_id)
            descriptor = resolve_without_network(
                connection.protocol,
                connection.base_url,
                connection.model,
                connection.limits,
            )
            environment = resolve_environment(
                descriptor,
                requested_output_tokens=self.settings.max_output_tokens,
                configured_trigger_tokens=connection.compact.trigger_input_tokens,
            )
            if connection.warning is not None:
                environment = replace(environment, warning=connection.warning)
            await self.state.provider.switch(connection, environment)
            return self.status()

    async def list_sessions(self) -> tuple[SessionSummary, ...]:
        catalog = SessionCatalog(self._project_state_dir)
        current_id = self.state.session.session_id
        return await _offload_session_io(
            lambda: catalog.list(exclude_session_id=current_id)
        )

    async def resume_session(self, session_id: str) -> ResumedSession:
        async with self.state.operation_lock():
            if session_id == self.state.session.session_id:
                raise ValueError("Session is already active")
            if self._pending_inputs.queued_inputs():
                raise RuntimeError("Recall or clear queued inputs before resuming")
            # Fully hydrate, validate, and project before publishing the candidate.
            session = await _offload_session_io(
                lambda: Session.restore(
                    self._project_state_dir,
                    session_id,
                    tool_results_dir=self.settings.paths.tool_results_dir(session_id),
                )
            )
            history = await _offload_session_io(lambda: self._project_history(session))
            permission_mode = (
                PermissionMode.PLAN
                if CollaborationMode(session.collaboration_mode)
                is CollaborationMode.PLAN
                else PermissionMode(session.permission_mode)
            )
            permission_policy = PermissionPolicy(
                permission_mode, self.state.permissions.policy.rules
            )
            restore_skill_permissions(permission_policy, session.conversation)
            self.state.replace_session(session, permission_policy=permission_policy)
            self._pending_inputs = PendingInputController(
                session.session_id, self.attachment_loader
            )
            if self.background_wake_signal is not None:
                self.background_wake_signal.pulse()
            return ResumedSession(status=self.status(), history=history)

    def _persist_permission_mode(self, mode: PermissionMode) -> bool:
        return self.state.session.set_permission_mode(mode.value)

    async def close(self) -> None:
        self._pending_inputs.clear()
        try:
            await self.permission_prompter.close()
            await self.question_broker.close()
            await self.state.close()
        finally:
            self._shutdown_observability()

    async def _load_attachments(self, prompt: str) -> tuple[AttachmentPayload, ...]:
        if self.attachment_loader is None:
            return ()
        loaded = await self.attachment_loader.load(prompt)
        return tuple(item.attachment for item in loaded)

    def _project_history(self, session: Session) -> tuple[HistoryEntry, ...]:
        catalog = self.state.tools.snapshot()
        tools = ToolExposureSnapshot.build(
            catalog,
            self.settings.tool_search_mode,
            restored_discoveries(session.conversation),
        )
        results = {
            block.tool_use_id: block
            for message in session.conversation
            if isinstance(message, ToolResultBatch)
            for block in message.content
            if isinstance(block, ToolResult)
        }
        history: list[HistoryEntry] = []
        context_groups = _history_context_groups(session)
        history.extend(context_groups.pop(None, ()))
        for message in session.conversation:
            if isinstance(message, HumanMessage):
                history.append(HistoryText("user", message.content))
            elif isinstance(message, ConversationSummaryMessage):
                history.append(HistoryText("system", "Conversation compacted"))
            elif isinstance(message, AssistantMessage):
                tool_ids = [
                    block.id for block in message.content if isinstance(block, ToolCall)
                ]
                for block in message.content:
                    if isinstance(block, TextContent) and block.text:
                        visible = strip_proposed_plan(block.text)
                        if visible.strip():
                            history.append(
                                HistoryText(
                                    "assistant",
                                    visible,
                                    is_final_answer=not tool_ids,
                                )
                            )
                        plan = extract_proposed_plan(block.text)
                        if plan:
                            history.append(HistoryPlan(plan))
                    elif isinstance(block, ReasoningContent):
                        history.append(HistoryReasoning(block.presentation))
                    elif isinstance(block, ToolCall):
                        result = results.get(block.id)
                        todos = None
                        semantic_call = unwrap_searched_tool_call(block)
                        if (
                            semantic_call.name == TODO_WRITE_TOOL_NAME
                            and result is not None
                            and not result.is_error
                        ):
                            try:
                                todos = parse_todo_input(semantic_call.input)
                            except (TypeError, ValueError):
                                pass
                        history.append(
                            HistoryToolCall(
                                tool_use_id=block.id,
                                use=self.tool_executor.present_use(block, tools=tools),
                                result=(
                                    result.presentation
                                    if result is not None
                                    else self.tool_executor.present_error(
                                        block,
                                        "Tool result is missing from the transcript.",
                                        tools=tools,
                                    )
                                ),
                                is_error=result is None or result.is_error,
                                todos=todos,
                                ends_tool_batch=bool(tool_ids)
                                and block.id == tool_ids[-1],
                                name=block.name,
                                input=block.input,
                            )
                        )
            history.extend(context_groups.pop(message.uuid, ()))
        for unmatched in context_groups.values():
            history.extend(unmatched)
        return tuple(history)


def _history_context_groups(
    session: Session,
) -> dict[str | None, tuple[HistoryContextGroup, ...]]:
    """Recover the same deduplicated Detailed injections used by live events."""

    grouped: dict[str | None, list[HistoryContextGroup]] = {}
    previous_refs: set[str] = set()
    visible_origins = {
        ModelInputOriginKind.USER_CONTEXT,
        ModelInputOriginKind.ATTACHMENT,
        ModelInputOriginKind.CONTENT_REPLACEMENT,
    }
    for request in session.request_audit_snapshot().requests:
        manifest = request.manifest
        items: list[HistoryContextItem] = []
        if manifest.purpose.value != "compact":
            for value, origin, audit_id in zip(
                request.input,
                manifest.origins,
                manifest.input_refs,
                strict=True,
            ):
                if audit_id in previous_refs or origin.kind not in visible_origins:
                    continue
                items.append(
                    HistoryContextItem(
                        origin.source or origin.kind.value,
                        origin.attachment_kind,
                        _audit_input_text(value),
                    )
                )
        previous_refs.update(manifest.input_refs)
        if items:
            grouped.setdefault(manifest.causal_head, []).append(
                HistoryContextGroup(manifest.request_number, tuple(items))
            )
    return {key: tuple(value) for key, value in grouped.items()}


def _audit_input_text(value: object) -> str:
    if not isinstance(value, dict) or value.get("type") != "user_input":
        return ""
    content = value.get("content")
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "input_text":
            continue
        text = block.get("text")
        if isinstance(text, str):
            parts.append(text)
    return "\n".join(parts)


def _transcript_value(
    value: object, *, omitted: frozenset[str] | set[str] = frozenset()
) -> TranscriptValue:
    """Freeze JSON-like and dataclass values into a frontend-neutral tree."""

    if is_dataclass(value) and not isinstance(value, type):
        return TranscriptValue(
            "object",
            fields=tuple(
                TranscriptField(
                    item.name,
                    _transcript_value(getattr(value, item.name), omitted=omitted),
                )
                for item in fields(value)
                if item.name != "kind" and item.name not in omitted
            ),
        )
    if isinstance(value, dict):
        return TranscriptValue(
            "object",
            fields=tuple(
                TranscriptField(str(key), _transcript_value(item, omitted=omitted))
                for key, item in value.items()
            ),
        )
    if isinstance(value, (list, tuple)):
        return TranscriptValue(
            "array",
            items=tuple(_transcript_value(item, omitted=omitted) for item in value),
        )
    if value is None:
        scalar = "null"
    elif value is True:
        scalar = "true"
    elif value is False:
        scalar = "false"
    else:
        scalar = str(value)
    return TranscriptValue("scalar", scalar=scalar)


def _project_turn_outcome(
    result: AgentTurnSucceeded | AgentMaxStepsReached,
) -> TurnOutcome:
    if isinstance(result, AgentTurnSucceeded):
        return TurnSucceeded(
            result.text,
            result.completed_steps,
            result.usage.input_tokens,
            result.usage.output_tokens,
        )
    return MaxStepsReached(
        result.max_steps,
        result.completed_steps,
        result.usage.input_tokens,
        result.usage.output_tokens,
    )


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
    "ChatService",
]
