"""Agent inbound port 与终端前端之间的 application adapter。"""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Protocol

from nano_code.agent import (
    AgentHistoryAssistantMessage,
    AgentHistorySystemMessage,
    AgentHistoryToolCall,
    AgentHistoryUserMessage,
    AgentMaxStepsReached,
    AgentSessionView,
    AgentStepLimitReached,
    AgentTextDelta,
    AgentTodoListUpdated,
    AgentToolFinished,
    AgentToolStarted,
    AgentTurnCompleted,
    AgentTurnInput,
    AgentTurnSucceeded,
)
from nano_code.agent.ports.inbound import AgentInboundPort
from nano_code.agent.ports.session import SessionRepository
from nano_code.attachments import AttachmentLoader, WorkspacePathSuggester
from nano_code.core import AgentSettings
from nano_code.messages import ContextAttachment, JsonObject
from nano_code.permissions import PermissionConfirmation
from nano_code.permissions.models import PermissionDecision
from nano_code.presentation import generic_tool_use_presentation
from nano_code.providers.manager import ProviderUpdate, ProviderView
from nano_code.providers.router import ProviderConnection
from nano_code.sessions import SessionSummary
from nano_code.tools import Tool
from nano_code.tui import (
    AttachmentLoaded,
    ContextStatus,
    HistoryAssistantMessage,
    HistoryEntry,
    HistorySystemMessage,
    HistoryToolCall,
    HistoryUserMessage,
    MaxStepsReached,
    PathSuggestion,
    PermissionHandler,
    PermissionRequest,
    ResumedSession,
    RuntimeStatus,
    StepLimitReached,
    TextDelta,
    TodoListUpdated,
    ToolFinished,
    ToolStarted,
    TurnCompleted,
    TurnEvent,
    TurnOutcome,
    TurnSucceeded,
)


class ProviderControlPort(Protocol):
    """CliChatRuntime 使用的 provider profile/application 能力。"""

    def providers(self, active_provider_id: str) -> tuple[ProviderView, ...]: ...

    async def configure(self, update: ProviderUpdate) -> ProviderConnection: ...


class SessionSourcePort(Protocol):
    """CliChatRuntime 使用的 session catalog 与 repository factory。"""

    def list(self, *, exclude_session_id: str) -> tuple[SessionSummary, ...]: ...

    def open(self, session_id: str) -> SessionRepository: ...


class DeferredPermissionPrompter:
    """在不导入具体前端的情况下，将核心权限检查桥接到当前前端。"""

    def __init__(self) -> None:
        self._handler: PermissionHandler | None = None

    def set_handler(self, handler: PermissionHandler) -> None:
        self._handler = handler

    async def confirm(
        self, tool: Tool, tool_input: JsonObject, decision: PermissionDecision
    ) -> PermissionConfirmation:
        if self._handler is None:
            return PermissionConfirmation(False)
        try:
            presentation = tool.present_use(tool_input)
        except Exception:
            presentation = generic_tool_use_presentation(
                tool.definition.name, tool_input
            )
        return await self._handler(
            PermissionRequest(
                tool_name=tool.definition.name,
                tool_input=tool_input,
                message=decision.message,
                presentation=presentation,
                suggestions=decision.suggestions,
            )
        )


class CliChatRuntime:
    """将 Agent inbound port 适配为 TUI 消费的窄接口。"""

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
                if isinstance(event, AgentTextDelta):
                    yield TextDelta(event.text)
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
        suggestions = await self.path_suggester.suggest(query)
        return tuple(
            PathSuggestion(item.path, item.is_directory, item.display)
            for item in suggestions
        )

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
        )

    async def compact(self) -> ContextStatus:
        async with self._session_lock:
            await self.agent.compact("manual")
            return self.context_status()

    def set_permission_handler(self, handler: PermissionHandler) -> None:
        self.permission_prompter.set_handler(handler)

    def providers(self) -> tuple[ProviderView, ...]:
        return self.provider_control.providers(self.settings.provider_id)

    async def configure_provider(self, update: ProviderUpdate) -> RuntimeStatus:
        connection = await self.provider_control.configure(update)
        self.settings = replace(
            self.settings,
            provider_id=connection.id,
            model=connection.model,
            base_url=connection.base_url,
            api_key=connection.api_key,
            credential_source=connection.credential_source,
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
