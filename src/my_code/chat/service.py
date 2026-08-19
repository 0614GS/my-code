"""Stateful user-level chat orchestration."""

from collections.abc import AsyncIterator

from my_code.agent.engine import AgentEngine
from my_code.agent.events import (
    AgentConversationUpdated,
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
from my_code.application.state import AppState
from my_code.chat.events import (
    AttachmentLoaded,
    MaxStepsReached,
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
    TurnOutcome,
    TurnSucceeded,
)
from my_code.chat.history import (
    HistoryEntry,
    HistoryReasoning,
    HistoryText,
    HistoryToolCall,
    ResumedSession,
)
from my_code.chat.permissions import DeferredPermissionPrompter, PermissionHandler
from my_code.chat.status import ContextStatus, RuntimeStatus
from my_code.config.settings import AgentSettings
from my_code.context.attachments.models import ContextAttachment
from my_code.context.engine import ContextEngine
from my_code.conversation.models import (
    AssistantMessage,
    ConversationSummaryMessage,
    HumanMessage,
    ReasoningContent,
    TextContent,
    ToolCall,
    ToolResult,
    ToolResultBatch,
)
from my_code.features.file_mentions.loader import AttachmentLoader
from my_code.features.file_mentions.models import PathSuggestion
from my_code.features.file_mentions.suggestions import WorkspacePathSuggester
from my_code.features.todos.projection import project_todos
from my_code.model.capabilities import (
    CapabilitySource,
    ModelDescriptor,
    resolve_environment,
)
from my_code.providers.discovery import resolve_without_network
from my_code.providers.manager import ProviderManager, ProviderUpdate, ProviderView
from my_code.sessions.catalog import SessionCatalog, SessionSummary
from my_code.sessions.session import Session
from my_code.tools.executor import ToolExecutor


class ChatService:
    """Coordinate chat use cases over the explicit AppState graph."""

    def __init__(
        self,
        *,
        agent: AgentEngine,
        context: ContextEngine,
        tool_executor: ToolExecutor,
        settings: AgentSettings,
        permission_prompter: DeferredPermissionPrompter,
        provider_manager: ProviderManager,
        state: AppState,
        attachment_loader: AttachmentLoader | None = None,
        path_suggester: WorkspacePathSuggester | None = None,
    ) -> None:
        self.agent = agent
        self.context = context
        self.tool_executor = tool_executor
        self.settings = settings
        self.permission_prompter = permission_prompter
        self.provider_manager = provider_manager
        self.state = state
        self._project_state_dir = settings.paths.project_state_dir
        self.attachment_loader = attachment_loader
        self.path_suggester = path_suggester or WorkspacePathSuggester(settings.cwd)

    async def submit(self, prompt: str) -> TurnOutcome:
        async with self.state.operation_lock():
            attachments = await self._load_attachments(prompt)
            result = await self.agent.submit(
                self.state.session,
                AgentTurnInput(prompt, attachments),
            )
        return _project_turn_outcome(result)

    async def stream(self, prompt: str) -> AsyncIterator[TurnEvent]:
        async with self.state.operation_lock():
            loaded = (
                await self.attachment_loader.load(prompt)
                if self.attachment_loader is not None
                else ()
            )
            for item in loaded:
                yield AttachmentLoaded(item.path, item.is_directory, item.display)
            session = self.state.session
            previous_todos = project_todos(session.snapshot().history).todos
            async for event in self.agent.stream(
                session,
                AgentTurnInput(prompt, tuple(item.attachment for item in loaded)),
            ):
                if isinstance(event, AgentTextStarted):
                    yield TextStarted()
                elif isinstance(event, AgentTextDelta):
                    yield TextDelta(event.text)
                elif isinstance(event, AgentTextCompleted):
                    yield TextCompleted(event.text)
                elif isinstance(event, AgentReasoningStarted):
                    yield ReasoningStarted(event.disclosure)
                elif isinstance(event, AgentReasoningDelta):
                    yield ReasoningDelta(event.disclosure, event.part_index, event.text)
                elif isinstance(event, AgentReasoningCompleted):
                    yield ReasoningCompleted(event.presentation)
                elif isinstance(event, AgentToolStarted):
                    yield ToolStarted(event.tool_use_id, event.presentation)
                elif isinstance(event, AgentToolFinished):
                    yield ToolFinished(
                        event.tool_use_id, event.is_error, event.presentation
                    )
                elif isinstance(event, AgentConversationUpdated):
                    current_todos = project_todos(session.snapshot().history).todos
                    if current_todos != previous_todos:
                        previous_todos = current_todos
                        yield TodoListUpdated(current_todos)
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
        return RuntimeStatus(
            session_id=session.session_id,
            cwd=str(self.settings.cwd),
            provider_id=connection.id,
            base_url=connection.base_url,
            model=connection.model,
            permission_mode=self.state.permissions.policy.mode.value,
            credential_source=connection.credential_source.value,
            working_message_count=session.message_count,
            todos=project_todos(session.snapshot().history).todos,
        )

    def context_status(self) -> ContextStatus:
        session = self.state.session
        budget = self.context.inspect(
            session.context_snapshot(),
            session,
        )
        return ContextStatus(
            estimated_input_tokens=budget.estimated_input_tokens,
            reserved_output_tokens=budget.reserved_output_tokens,
            estimated_total_tokens=budget.estimated_total_tokens,
            message_chars=budget.message_chars,
            system_chars=budget.system_chars,
            tool_schema_chars=budget.tool_schema_chars,
            user_context_chars=budget.user_context_chars,
            attachment_chars=budget.attachment_chars,
            message_limit_chars=budget.message_limit_chars,
            working_message_count=session.message_count,
            replacement_count=session.content_replacement_count,
            compact_count=session.compact_count,
            input_tokens=budget.input_tokens,
            input_limit_tokens=budget.input_limit_tokens,
            compact_trigger_tokens=budget.compact_trigger_tokens,
            remaining_input_tokens=budget.remaining_input_tokens,
            measurement=budget.measurement,
            model_limit_source=budget.model_limit_source.value,
            configured_compact_trigger_tokens=budget.configured_compact_trigger_tokens,
            warning=budget.warning,
        )

    async def compact(self) -> ContextStatus:
        async with self.state.operation_lock():
            session = self.state.session
            outcome = await self.context.compact(
                session.context_snapshot(),
                "manual",
            )
            session.commit_compaction(
                outcome.replacements,
                outcome.summary,
                outcome.boundary,
            )
            return self.context_status()

    def set_permission_handler(self, handler: PermissionHandler) -> None:
        self.permission_prompter.set_handler(handler)

    def providers(self) -> tuple[ProviderView, ...]:
        return self.provider_manager.list(self.state.provider.router.connection.id)

    async def refresh_provider_models(self, provider_id: str) -> ProviderView:
        async with self.state.operation_lock():
            view = await self.provider_manager.refresh_models(provider_id)
            connection = self.state.provider.router.connection
            if connection.id == provider_id:
                source = (
                    CapabilitySource(view.capability_source)
                    if view.capability_source is not None
                    else CapabilitySource.FALLBACK
                )
                self.state.provider.update_environment(
                    resolve_environment(
                        ModelDescriptor(
                            view.model,
                            view.model,
                            view.resolved_limits,
                            source=source,
                        ),
                        requested_output_tokens=self.settings.max_output_tokens,
                        configured_trigger_tokens=(
                            connection.compact.trigger_input_tokens
                        ),
                        discovered_at=view.discovered_at,
                        discovery_error=view.discovery_error,
                    )
                )
            return view

    async def configure_provider(self, update: ProviderUpdate) -> RuntimeStatus:
        async with self.state.operation_lock():
            connection = self.provider_manager.configure(update)
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
            await self.state.provider.switch(connection, environment)
            return self.status()

    async def list_sessions(self) -> tuple[SessionSummary, ...]:
        return SessionCatalog(self._project_state_dir).list(
            exclude_session_id=self.state.session.session_id
        )

    async def resume_session(self, session_id: str) -> ResumedSession:
        async with self.state.operation_lock():
            if session_id == self.state.session.session_id:
                raise ValueError("Session is already active")
            # Fully hydrate and repair every candidate component before publishing it.
            session = Session.restore(self._project_state_dir, session_id)
            history = self._project_history(session)
            self.state.replace_session(session)
            return ResumedSession(status=self.status(), history=history)

    async def close(self) -> None:
        await self.permission_prompter.close()
        await self.state.close()

    async def _load_attachments(self, prompt: str) -> tuple[ContextAttachment, ...]:
        if self.attachment_loader is None:
            return ()
        loaded = await self.attachment_loader.load(prompt)
        return tuple(item.attachment for item in loaded)

    def _project_history(self, session: Session) -> tuple[HistoryEntry, ...]:
        results = {
            block.tool_use_id: block
            for message in session.snapshot().history
            if isinstance(message, ToolResultBatch)
            for block in message.content
            if isinstance(block, ToolResult)
        }
        history: list[HistoryEntry] = []
        for message in session.snapshot().history:
            if isinstance(message, HumanMessage):
                history.append(HistoryText("user", message.content))
            elif isinstance(message, ConversationSummaryMessage):
                history.append(HistoryText("system", "Conversation compacted"))
            elif isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextContent) and block.text:
                        history.append(HistoryText("assistant", block.text))
                    elif isinstance(block, ReasoningContent):
                        history.append(HistoryReasoning(block.presentation))
                    elif isinstance(block, ToolCall):
                        result = results.get(block.id)
                        history.append(
                            HistoryToolCall(
                                tool_use_id=block.id,
                                use=self.tool_executor.present_use(block),
                                result=(
                                    session.tool_presentation(block.id)
                                    or self.tool_executor.present_stored_result(
                                        block, result
                                    )
                                ),
                                is_error=result is None or result.is_error,
                            )
                        )
        return tuple(history)


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


__all__ = [
    "ChatService",
]
