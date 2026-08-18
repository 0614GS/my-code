"""Default interactive chat application runtime."""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Protocol

from nano_code.agent import (
    AgentHistoryAssistantMessage,
    AgentHistoryReasoning,
    AgentHistorySystemMessage,
    AgentHistoryToolCall,
    AgentHistoryUserMessage,
    AgentMaxStepsReached,
    AgentReasoningCompleted,
    AgentReasoningDelta,
    AgentReasoningStarted,
    AgentSessionView,
    AgentStepLimitReached,
    AgentTextCompleted,
    AgentTextDelta,
    AgentTextStarted,
    AgentTodoListUpdated,
    AgentToolFinished,
    AgentToolStarted,
    AgentTurnCompleted,
    AgentTurnInput,
    AgentTurnSucceeded,
)
from nano_code.agent.ports.inbound import AgentInboundPort
from nano_code.agent.ports.session import SessionRepository
from nano_code.application.chat.contracts import (
    AttachmentLoaded,
    ContextStatus,
    HistoryAssistantMessage,
    HistoryEntry,
    HistoryReasoning,
    HistorySystemMessage,
    HistoryToolCall,
    HistoryUserMessage,
    MaxStepsReached,
    PathSuggestion,
    PermissionHandler,
    ReasoningCompleted,
    ReasoningDelta,
    ReasoningStarted,
    ResumedSession,
    RuntimeStatus,
    StepLimitReached,
    TextCompleted,
    TextDelta,
    TextStarted,
    TodoListUpdated,
    ToolFinished,
    ToolStarted,
    TurnCompleted,
    TurnEvent,
    TurnOutcome,
    TurnSucceeded,
)
from nano_code.application.chat.permissions import DeferredPermissionPrompter
from nano_code.context.attachments.models import ContextAttachment
from nano_code.core import AgentSettings
from nano_code.features.file_mentions import AttachmentLoader, WorkspacePathSuggester
from nano_code.providers.manager import ProviderUpdate, ProviderView
from nano_code.providers.router import ProviderConnection
from nano_code.sessions import SessionSummary


class ProviderControlPort(Protocol):
    """DefaultChatRuntime 使用的 provider profile/application 能力。"""

    def providers(self, active_provider_id: str) -> tuple[ProviderView, ...]: ...

    async def configure(self, update: ProviderUpdate) -> ProviderConnection: ...

    async def refresh_models(self, provider_id: str) -> ProviderView: ...


class SessionSourcePort(Protocol):
    """DefaultChatRuntime 使用的 session catalog 与 repository factory。"""

    def list(self, *, exclude_session_id: str) -> tuple[SessionSummary, ...]: ...

    def open(self, session_id: str) -> SessionRepository: ...


class DefaultChatRuntime:
    """Adapt the Agent inbound port to the interactive chat contract."""

    def __init__(
        self,
        agent: AgentInboundPort,
        settings: AgentSettings,
        permission_prompter: DeferredPermissionPrompter,
        provider_control: ProviderControlPort,
        session_source: SessionSourcePort,
        attachment_loader: AttachmentLoader | None = None,
        path_suggester: WorkspacePathSuggester | None = None,
    ) -> None:
        self.agent = agent
        self.settings = settings
        self.permission_prompter = permission_prompter
        self.provider_control = provider_control
        self.session_source = session_source
        self.attachment_loader = attachment_loader
        self.path_suggester = path_suggester or WorkspacePathSuggester(settings.cwd)
        # 会话切换与用户回合共享同一把锁，避免 JSONL 归属在流式响应途中改变。
        self._session_lock = asyncio.Lock()

    async def submit(self, prompt: str) -> TurnOutcome:
        attachments = await self._load_attachments(prompt)
        async with self._session_lock:
            result = await self.agent.submit(AgentTurnInput(prompt, attachments))
        return _project_turn_outcome(result)

    async def stream(self, prompt: str) -> AsyncIterator[TurnEvent]:
        loaded = (
            await self.attachment_loader.load(prompt)
            if self.attachment_loader is not None
            else ()
        )
        for item in loaded:
            yield AttachmentLoaded(item.path, item.is_directory, item.display)
        async with self._session_lock:
            async for event in self.agent.stream(
                AgentTurnInput(prompt, tuple(item.attachment for item in loaded))
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
                        event.tool_use_id,
                        event.is_error,
                        event.presentation,
                    )
                elif isinstance(event, AgentTodoListUpdated):
                    yield TodoListUpdated(event.todos)
                elif isinstance(event, AgentTurnCompleted):
                    result = event.result
                    yield TurnCompleted(
                        TurnSucceeded(
                            text=result.text,
                            completed_steps=result.completed_steps,
                            input_tokens=result.usage.input_tokens,
                            output_tokens=result.usage.output_tokens,
                        )
                    )
                elif isinstance(event, AgentStepLimitReached):
                    limit = event.result
                    yield StepLimitReached(
                        MaxStepsReached(
                            max_steps=limit.max_steps,
                            completed_steps=limit.completed_steps,
                            input_tokens=limit.usage.input_tokens,
                            output_tokens=limit.usage.output_tokens,
                        )
                    )

    async def suggest_paths(self, query: str) -> tuple[PathSuggestion, ...]:
        return await self.path_suggester.suggest(query)

    async def _load_attachments(self, prompt: str) -> tuple[ContextAttachment, ...]:
        if self.attachment_loader is None:
            return ()
        loaded = await self.attachment_loader.load(prompt)
        return tuple(item.attachment for item in loaded)

    def status(self) -> RuntimeStatus:
        agent_status = self.agent.status()
        return RuntimeStatus(
            session_id=agent_status.session_id,
            cwd=str(self.settings.cwd),
            provider_id=self.settings.provider_id,
            base_url=self.settings.base_url,
            model=self.settings.model,
            permission_mode=self.settings.permission_mode.value,
            credential_source=self.settings.credential_source.value,
            working_message_count=agent_status.working_message_count,
            todos=agent_status.todos,
        )

    def context_status(self) -> ContextStatus:
        status = self.agent.context_status()
        budget = status.budget
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
            working_message_count=status.working_message_count,
            replacement_count=status.replacement_count,
            compact_count=status.compact_count,
            input_tokens=budget.input_tokens,
            input_limit_tokens=budget.input_limit_tokens,
            compact_trigger_tokens=budget.compact_trigger_tokens,
            remaining_input_tokens=budget.remaining_input_tokens,
            measurement=budget.measurement,
            model_limit_source=budget.model_limit_source.value,
            configured_compact_trigger_tokens=(
                budget.configured_compact_trigger_tokens
            ),
            warning=budget.warning,
        )

    async def compact(self) -> ContextStatus:
        async with self._session_lock:
            await self.agent.compact("manual")
            return self.context_status()

    def set_permission_handler(self, handler: PermissionHandler) -> None:
        self.permission_prompter.set_handler(handler)

    def providers(self) -> tuple[ProviderView, ...]:
        return self.provider_control.providers(self.settings.provider_id)

    async def refresh_provider_models(self, provider_id: str) -> ProviderView:
        return await self.provider_control.refresh_models(provider_id)

    async def configure_provider(self, update: ProviderUpdate) -> RuntimeStatus:
        connection = await self.provider_control.configure(update)
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
        return self.session_source.list(
            exclude_session_id=self.agent.status().session_id
        )

    async def resume_session(self, session_id: str) -> ResumedSession:
        async with self._session_lock:
            if session_id == self.agent.status().session_id:
                raise ValueError("Session is already active")
            view = self.agent.resume(self.session_source.open(session_id))
            return ResumedSession(
                status=self._status_for(view),
                history=self._project_history(view),
            )

    def _status_for(self, view: AgentSessionView) -> RuntimeStatus:
        status = view.status
        return RuntimeStatus(
            session_id=status.session_id,
            cwd=str(self.settings.cwd),
            provider_id=self.settings.provider_id,
            base_url=self.settings.base_url,
            model=self.settings.model,
            permission_mode=self.settings.permission_mode.value,
            credential_source=self.settings.credential_source.value,
            working_message_count=status.working_message_count,
            todos=status.todos,
        )

    @staticmethod
    def _project_history(view: AgentSessionView) -> tuple[HistoryEntry, ...]:
        history: list[HistoryEntry] = []
        for entry in view.history:
            if isinstance(entry, AgentHistoryUserMessage):
                history.append(HistoryUserMessage(entry.text))
            elif isinstance(entry, AgentHistoryAssistantMessage):
                history.append(HistoryAssistantMessage(entry.text))
            elif isinstance(entry, AgentHistoryReasoning):
                history.append(HistoryReasoning(entry.presentation))
            elif isinstance(entry, AgentHistorySystemMessage):
                history.append(HistorySystemMessage(entry.text))
            elif isinstance(entry, AgentHistoryToolCall):
                history.append(
                    HistoryToolCall(
                        tool_use_id=entry.tool_use_id,
                        use=entry.use,
                        result=entry.result,
                        is_error=entry.is_error,
                    )
                )
        return tuple(history)


def _project_turn_outcome(
    result: AgentTurnSucceeded | AgentMaxStepsReached,
) -> TurnOutcome:
    if isinstance(result, AgentTurnSucceeded):
        return TurnSucceeded(
            text=result.text,
            completed_steps=result.completed_steps,
            input_tokens=result.usage.input_tokens,
            output_tokens=result.usage.output_tokens,
        )
    return MaxStepsReached(
        max_steps=result.max_steps,
        completed_steps=result.completed_steps,
        input_tokens=result.usage.input_tokens,
        output_tokens=result.usage.output_tokens,
    )
