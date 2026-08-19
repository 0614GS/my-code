"""Stateful user-level chat orchestration."""

import asyncio
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, replace

from nano_code.agent.engine import AgentEngine
from nano_code.agent.events import (
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
from nano_code.agent.models import (
    AgentMaxStepsReached,
    AgentTurnInput,
    AgentTurnSucceeded,
)
from nano_code.chat.events import (
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
from nano_code.chat.history import (
    HistoryEntry,
    HistoryReasoning,
    HistoryText,
    HistoryToolCall,
    ResumedSession,
)
from nano_code.chat.permissions import DeferredPermissionPrompter, PermissionHandler
from nano_code.chat.status import ContextStatus, RuntimeStatus
from nano_code.config.settings import AgentSettings
from nano_code.context.attachments.models import ContextAttachment
from nano_code.context.session import ContextSession
from nano_code.conversation.models import (
    AssistantMessage,
    ConversationSummaryMessage,
    HumanMessage,
    ReasoningContent,
    TextContent,
    ToolCall,
    ToolResult,
    ToolResultsMessage,
)
from nano_code.features.file_mentions.loader import AttachmentLoader
from nano_code.features.file_mentions.models import PathSuggestion
from nano_code.features.file_mentions.suggestions import WorkspacePathSuggester
from nano_code.features.todos.projection import project_todos
from nano_code.model.capabilities import (
    ActiveModelState,
    CapabilitySource,
    ModelDescriptor,
    resolve_environment,
)
from nano_code.providers.discovery import resolve_without_network
from nano_code.providers.manager import ProviderManager, ProviderUpdate, ProviderView
from nano_code.providers.router import ProviderRouter
from nano_code.sessions.catalog import SessionCatalog, SessionSummary
from nano_code.sessions.session import Session
from nano_code.sessions.store import SessionStore
from nano_code.tools.result_store import ToolResultStore


@dataclass(frozen=True, slots=True)
class _SessionBundle:
    """All mutable state whose lifetime is exactly one active session."""

    session: Session
    context: ContextSession
    tool_results: ToolResultStore


class ChatService:
    """Own the active session bundle and project Agent events for hosts."""

    def __init__(
        self,
        *,
        agent: AgentEngine,
        settings: AgentSettings,
        permission_prompter: DeferredPermissionPrompter,
        provider_manager: ProviderManager,
        provider_router: ProviderRouter,
        active_model_state: ActiveModelState,
        session: Session,
        tool_result_store: Callable[[str], ToolResultStore],
        attachment_loader: AttachmentLoader | None = None,
        path_suggester: WorkspacePathSuggester | None = None,
    ) -> None:
        self.agent = agent
        self.settings = settings
        self.permission_prompter = permission_prompter
        self.provider_manager = provider_manager
        self.provider_router = provider_router
        self.active_model_state = active_model_state
        self._project_state_dir = settings.paths.project_state_dir
        self._tool_result_store = tool_result_store
        self.attachment_loader = attachment_loader
        self.path_suggester = path_suggester or WorkspacePathSuggester(settings.cwd)
        self._active = _SessionBundle(
            session, ContextSession(), tool_result_store(session.session_id)
        )
        # submit, stream, compact, resume and provider switching share one boundary.
        self._lock = asyncio.Lock()

    async def submit(self, prompt: str) -> TurnOutcome:
        async with self._lock:
            attachments = await self._load_attachments(prompt)
            active = self._active
            result = await self.agent.submit(
                active.session,
                active.context,
                active.tool_results,
                AgentTurnInput(prompt, attachments),
            )
        return _project_turn_outcome(result)

    async def stream(self, prompt: str) -> AsyncIterator[TurnEvent]:
        async with self._lock:
            loaded = (
                await self.attachment_loader.load(prompt)
                if self.attachment_loader is not None
                else ()
            )
            for item in loaded:
                yield AttachmentLoaded(item.path, item.is_directory, item.display)
            active = self._active
            previous_todos = project_todos(active.session.history).todos
            async for event in self.agent.stream(
                active.session,
                active.context,
                active.tool_results,
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
                    current_todos = project_todos(active.session.history).todos
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
        session = self._active.session
        return RuntimeStatus(
            session_id=session.session_id,
            cwd=str(self.settings.cwd),
            provider_id=self.settings.provider_id,
            base_url=self.settings.base_url,
            model=self.settings.model,
            permission_mode=self.settings.permission_mode.value,
            credential_source=self.settings.credential_source.value,
            working_message_count=session.message_count,
            todos=project_todos(session.history).todos,
        )

    def context_status(self) -> ContextStatus:
        active = self._active
        budget = self.agent.inspect(active.session, active.context)
        session = active.session
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
        async with self._lock:
            active = self._active
            await self.agent.compact(active.session, active.context, "manual")
            return self.context_status()

    def set_permission_handler(self, handler: PermissionHandler) -> None:
        self.permission_prompter.set_handler(handler)

    def providers(self) -> tuple[ProviderView, ...]:
        return self.provider_manager.list(self.settings.provider_id)

    async def refresh_provider_models(self, provider_id: str) -> ProviderView:
        async with self._lock:
            view = await self.provider_manager.refresh_models(provider_id)
            if self.provider_router.connection.id == provider_id:
                source = (
                    CapabilitySource(view.capability_source)
                    if view.capability_source is not None
                    else CapabilitySource.FALLBACK
                )
                self.active_model_state.set(
                    resolve_environment(
                        ModelDescriptor(
                            view.model,
                            view.model,
                            view.resolved_limits,
                            source=source,
                        ),
                        requested_output_tokens=self.settings.max_output_tokens,
                        configured_trigger_tokens=(
                            self.provider_router.connection.compact.trigger_input_tokens
                        ),
                        discovered_at=view.discovered_at,
                        discovery_error=view.discovery_error,
                    )
                )
            return view

    async def configure_provider(self, update: ProviderUpdate) -> RuntimeStatus:
        async with self._lock:
            connection = self.provider_manager.configure(update)
            await self.provider_router.switch(connection)
            descriptor = resolve_without_network(
                connection.protocol,
                connection.base_url,
                connection.model,
                connection.limits,
            )
            self.active_model_state.set(
                resolve_environment(
                    descriptor,
                    requested_output_tokens=self.settings.max_output_tokens,
                    configured_trigger_tokens=connection.compact.trigger_input_tokens,
                )
            )
            self.settings = replace(
                self.settings,
                provider_id=connection.id,
                model=connection.model,
                base_url=connection.base_url,
                api_key=connection.api_key,
                credential_source=connection.credential_source,
                protocol=connection.protocol,
                reasoning=connection.reasoning,
            )
            return self.status()

    async def list_sessions(self) -> tuple[SessionSummary, ...]:
        return SessionCatalog(self._project_state_dir).list(
            exclude_session_id=self._active.session.session_id
        )

    async def resume_session(self, session_id: str) -> ResumedSession:
        async with self._lock:
            if session_id == self._active.session.session_id:
                raise ValueError("Session is already active")
            # Fully hydrate and repair every candidate component before publishing it.
            session = Session.restore(SessionStore(self._project_state_dir, session_id))
            candidate = _SessionBundle(
                session,
                ContextSession(),
                self._tool_result_store(session.session_id),
            )
            history = self._project_history(candidate.session)
            self._active = candidate
            return ResumedSession(status=self.status(), history=history)

    async def _load_attachments(self, prompt: str) -> tuple[ContextAttachment, ...]:
        if self.attachment_loader is None:
            return ()
        loaded = await self.attachment_loader.load(prompt)
        return tuple(item.attachment for item in loaded)

    def _project_history(self, session: Session) -> tuple[HistoryEntry, ...]:
        results = {
            block.tool_use_id: block
            for message in session.history
            if isinstance(message, ToolResultsMessage)
            for block in message.content
            if isinstance(block, ToolResult)
        }
        history: list[HistoryEntry] = []
        for message in session.history:
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
                                use=self.agent.present_use(block),
                                result=(
                                    session.tool_presentation(block.id)
                                    or self.agent.present_stored_result(block, result)
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
