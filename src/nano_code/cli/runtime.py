"""CLI composition root 与 TUI 使用的 application adapter。"""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from nano_code.agent import (
    AgentEngine,
    AgentHistoryAssistantMessage,
    AgentHistorySystemMessage,
    AgentHistoryToolCall,
    AgentHistoryUserMessage,
    AgentSessionView,
    AgentTextDelta,
    AgentToolFinished,
    AgentToolStarted,
    AgentTurnCompleted,
    ConversationState,
)
from nano_code.agent.ports.inbound import AgentInboundPort
from nano_code.agent.ports.session import SessionRepository
from nano_code.config import NanoCodePaths, Settings
from nano_code.context import (
    AgentsUserContextResolver,
    CompactionCoordinator,
    ContextPlanner,
    ContextWindow,
)
from nano_code.context.compaction import CompactionService
from nano_code.messages import JsonObject
from nano_code.permissions import PermissionConfirmation, PermissionPolicy
from nano_code.permissions.models import PermissionDecision
from nano_code.permissions.prompt import (
    HeadlessPrompter,
    PermissionPrompter,
    TerminalPrompter,
)
from nano_code.presentation import generic_tool_use_presentation
from nano_code.prompts import build_system_prompt_registry
from nano_code.providers.manager import ProviderManager, ProviderUpdate, ProviderView
from nano_code.providers.profiles import ProviderProtocol
from nano_code.providers.router import ProviderConnection, ProviderRouter
from nano_code.sessions import SessionCatalog, SessionStore, SessionSummary
from nano_code.tools import Tool, ToolContext, ToolRegistry
from nano_code.tools.builtin import builtin_tools
from nano_code.tools.executor import ToolExecutor
from nano_code.tools.result_store import ToolResultStore
from nano_code.tools.round_executor import ToolRoundExecutor
from nano_code.tui import (
    ContextStatus,
    HistoryAssistantMessage,
    HistoryEntry,
    HistorySystemMessage,
    HistoryToolCall,
    HistoryUserMessage,
    PermissionHandler,
    PermissionRequest,
    ResumedSession,
    RuntimeStatus,
    TextDelta,
    ToolFinished,
    ToolStarted,
    TurnCompleted,
    TurnEvent,
    TurnResult,
)


class ProviderControlPort(Protocol):
    """CliChatRuntime 使用的 provider profile/application 能力。"""

    def providers(self, active_provider_id: str) -> tuple[ProviderView, ...]: ...

    async def configure(self, update: ProviderUpdate) -> ProviderConnection: ...


class SessionSourcePort(Protocol):
    """CliChatRuntime 使用的 session catalog 与 repository factory。"""

    def list(self, *, exclude_session_id: str) -> tuple[SessionSummary, ...]: ...

    def open(self, session_id: str) -> SessionRepository: ...


class CliProviderController:
    """把 profile 持久化和活跃 ProviderRouter 切换组合成 CLI adapter。"""

    def __init__(self, paths: NanoCodePaths, router: ProviderRouter) -> None:
        self._manager = ProviderManager(paths)
        self._router = router

    def providers(self, active_provider_id: str) -> tuple[ProviderView, ...]:
        return self._manager.list(active_provider_id)

    async def configure(self, update: ProviderUpdate) -> ProviderConnection:
        connection = self._manager.configure(update)
        await self._router.switch(connection)
        return connection


class ProjectSessionSource:
    """当前项目的 session 发现与 JSONL repository factory。"""

    def __init__(self, project_state_dir: Path) -> None:
        self._project_state_dir = project_state_dir
        self._catalog = SessionCatalog(project_state_dir)

    def list(self, *, exclude_session_id: str) -> tuple[SessionSummary, ...]:
        return self._catalog.list(exclude_session_id=exclude_session_id)

    def open(self, session_id: str) -> SessionRepository:
        return SessionStore(self._project_state_dir, session_id)


def _build_engine_parts(
    settings: Settings,
    session_id: str | None = None,
    *,
    permission_prompter: PermissionPrompter | None = None,
) -> tuple[AgentEngine, ProviderRouter]:
    """只在 composition root 装配具体 SDK、文件系统和内置工具。"""

    actual_session_id = session_id or str(uuid4())
    session_store = SessionStore(settings.paths.project_state_dir, actual_session_id)
    registry = ToolRegistry(builtin_tools())
    # stdin/stdout 无法可靠展示确认提示时，无头模式权限请求按拒绝处理，
    # 而不是假定用户已经同意。
    prompter = permission_prompter or (
        TerminalPrompter() if settings.interactive else HeadlessPrompter()
    )
    tool_executor = ToolExecutor(
        registry=registry,
        policy=PermissionPolicy(mode=settings.permission_mode),
        prompter=prompter,
        context=ToolContext(cwd=settings.cwd),
        result_store=ToolResultStore(
            settings.paths.tool_results_dir(actual_session_id)
        ),
    )
    provider = ProviderRouter(
        ProviderConnection(
            id=settings.provider_id,
            protocol=ProviderProtocol.ANTHROPIC_MESSAGES,
            model=settings.model,
            base_url=settings.base_url,
            api_key=settings.api_key,
            credential_source=settings.credential_source,
        )
    )
    context = ContextPlanner(
        window=ContextWindow(settings.context_chars),
        prompt=build_system_prompt_registry(settings.cwd),
        tools=registry.definitions,
        max_output_tokens=settings.max_output_tokens,
        user_context_resolver=AgentsUserContextResolver(settings.cwd),
    )
    tool_round = ToolRoundExecutor(
        tool_executor,
        result_store_factory=lambda active_id: ToolResultStore(
            settings.paths.tool_results_dir(active_id)
        ),
    )
    engine = AgentEngine(
        model_turn=provider,
        tool_round=tool_round,
        conversation=ConversationState(session_store),
        context=context,
        compactor=CompactionCoordinator(context, CompactionService(provider)),
        max_turns=settings.max_turns,
    )
    return engine, provider


def build_engine(
    settings: Settings,
    session_id: str | None = None,
    *,
    permission_prompter: PermissionPrompter | None = None,
) -> AgentEngine:
    """围绕可测试核心装配 Agent inbound port 的具体实现。"""

    engine, _ = _build_engine_parts(
        settings,
        session_id,
        permission_prompter=permission_prompter,
    )
    return engine


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
            )
        )


def build_runtime(
    settings: Settings,
    session_id: str | None = None,
    *,
    permission_prompter: DeferredPermissionPrompter | None = None,
) -> "CliChatRuntime":
    """构造 TUI 使用的 application adapter。"""

    prompter = permission_prompter or DeferredPermissionPrompter()
    engine, provider = _build_engine_parts(
        settings,
        session_id,
        permission_prompter=prompter,
    )
    return CliChatRuntime(
        agent=engine,
        settings=settings,
        permission_prompter=prompter,
        provider_control=CliProviderController(settings.paths, provider),
        session_source=ProjectSessionSource(settings.paths.project_state_dir),
    )


class CliChatRuntime:
    """将 Agent inbound port 适配为 TUI 消费的窄接口。"""

    def __init__(
        self,
        agent: AgentInboundPort,
        settings: Settings,
        permission_prompter: DeferredPermissionPrompter,
        provider_control: ProviderControlPort,
        session_source: SessionSourcePort,
    ) -> None:
        self.agent = agent
        self.settings = settings
        self.permission_prompter = permission_prompter
        self.provider_control = provider_control
        self.session_source = session_source
        # 会话切换与模型轮次共享同一把锁，避免 JSONL 归属在流式响应途中改变。
        self._session_lock = asyncio.Lock()

    async def submit(self, prompt: str) -> TurnResult:
        async with self._session_lock:
            result = await self.agent.submit(prompt)
        return TurnResult(
            text=result.text,
            turns=result.turns,
            input_tokens=result.usage.input_tokens,
            output_tokens=result.usage.output_tokens,
        )

    async def stream(self, prompt: str) -> AsyncIterator[TurnEvent]:
        async with self._session_lock:
            async for event in self.agent.stream(prompt):
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
                elif isinstance(event, AgentTurnCompleted):
                    result = event.result
                    yield TurnCompleted(
                        TurnResult(
                            text=result.text,
                            turns=result.turns,
                            input_tokens=result.usage.input_tokens,
                            output_tokens=result.usage.output_tokens,
                        )
                    )

    def status(self) -> RuntimeStatus:
        return RuntimeStatus(
            session_id=self.agent.session_id,
            cwd=str(self.settings.cwd),
            provider_id=self.settings.provider_id,
            base_url=self.settings.base_url,
            model=self.settings.model,
            permission_mode=self.settings.permission_mode.value,
            credential_source=self.settings.credential_source.value,
            message_count=self.agent.message_count,
        )

    def context_status(self) -> ContextStatus:
        state = self.agent.context_state()
        budget = state.budget
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
            working_message_count=state.working_message_count,
            replacement_count=state.replacement_count,
            compact_count=state.compact_count,
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
        return self.session_source.list(exclude_session_id=self.agent.session_id)

    async def resume_session(self, session_id: str) -> ResumedSession:
        async with self._session_lock:
            if session_id == self.agent.session_id:
                raise ValueError("Session is already active")
            view = self.agent.resume(self.session_source.open(session_id))
            return ResumedSession(
                status=self._status_for(view),
                history=self._project_history(view),
            )

    def _status_for(self, view: AgentSessionView) -> RuntimeStatus:
        state = view.state
        return RuntimeStatus(
            session_id=state.session_id,
            cwd=str(self.settings.cwd),
            provider_id=self.settings.provider_id,
            base_url=self.settings.base_url,
            model=self.settings.model,
            permission_mode=self.settings.permission_mode.value,
            credential_source=self.settings.credential_source.value,
            message_count=state.message_count,
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
