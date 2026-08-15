"""CLI 依赖的组合根。"""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace
from uuid import uuid4

from nano_code.agent import (
    AgentEngine,
    AgentTextDelta,
    AgentToolFinished,
    AgentToolStarted,
    AgentTurnCompleted,
)
from nano_code.config import Settings
from nano_code.context import (
    ContextPlanner,
    ContextWindow,
)
from nano_code.context.compaction import CompactionService
from nano_code.messages import (
    ChatMessage,
    JsonObject,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    to_json_object,
)
from nano_code.permissions import PermissionConfirmation, PermissionPolicy
from nano_code.permissions.models import PermissionDecision
from nano_code.permissions.prompt import (
    HeadlessPrompter,
    PermissionPrompter,
    TerminalPrompter,
)
from nano_code.presentation import generic_tool_use_presentation
from nano_code.prompts import default_prompt_registry
from nano_code.providers.manager import ProviderManager, ProviderUpdate, ProviderView
from nano_code.providers.profiles import ProviderProtocol
from nano_code.providers.router import ProviderConnection, ProviderRouter
from nano_code.sessions import SessionCatalog, SessionStore, SessionSummary
from nano_code.tools import Tool, ToolContext, ToolRegistry
from nano_code.tools.builtin import builtin_tools
from nano_code.tools.executor import ToolExecutor
from nano_code.tools.result_store import ToolResultStore
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


def build_engine(
    settings: Settings,
    session_id: str | None = None,
    *,
    permission_prompter: PermissionPrompter | None = None,
) -> AgentEngine:
    """围绕可测试核心装配具体 CLI 适配器。"""

    # 将具体 SDK、终端和文件系统实现集中在组合根中。
    # 智能体循环本身只依赖协议，可使用 fake 实现测试。
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
    return AgentEngine(
        provider=provider,
        tool_executor=tool_executor,
        session_store=session_store,
        context_planner=ContextPlanner(
            window=ContextWindow(settings.context_chars),
            prompt=default_prompt_registry(settings.cwd),
            tools=registry.definitions,
            max_output_tokens=settings.max_output_tokens,
        ),
        compaction_service=CompactionService(provider),
        max_turns=settings.max_turns,
    )


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


class CliChatRuntime:
    """将具体引擎适配为 TUI 消费的窄接口。"""

    def __init__(
        self,
        engine: AgentEngine,
        settings: Settings,
        permission_prompter: DeferredPermissionPrompter,
    ) -> None:
        self.engine = engine
        self.settings = settings
        self.permission_prompter = permission_prompter
        if not isinstance(engine.provider, ProviderRouter):
            raise TypeError("CliChatRuntime requires a ProviderRouter")
        self.provider_router = engine.provider
        self.provider_manager = ProviderManager(settings.paths)
        self.session_catalog = SessionCatalog(settings.paths.project_state_dir)
        # 会话切换与模型轮次共享同一把锁，避免 JSONL 归属在流式响应途中改变。
        self._session_lock = asyncio.Lock()

    async def submit(self, prompt: str) -> TurnResult:
        async with self._session_lock:
            result = await self.engine.submit(prompt)
        return TurnResult(
            text=result.text,
            turns=result.turns,
            input_tokens=result.usage.input_tokens,
            output_tokens=result.usage.output_tokens,
        )

    async def stream(self, prompt: str) -> AsyncIterator[TurnEvent]:
        async with self._session_lock:
            async for event in self.engine.submit_stream(prompt):
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
            session_id=self.engine.session_store.session_id,
            cwd=str(self.settings.cwd),
            provider_id=self.settings.provider_id,
            base_url=self.settings.base_url,
            model=self.settings.model,
            permission_mode=self.settings.permission_mode.value,
            credential_source=self.settings.credential_source.value,
            message_count=len(self.engine.messages),
        )

    def context_status(self) -> ContextStatus:
        budget = self.engine.context_budget()
        return ContextStatus(
            estimated_input_tokens=budget.estimated_input_tokens,
            reserved_output_tokens=budget.reserved_output_tokens,
            estimated_total_tokens=budget.estimated_total_tokens,
            message_chars=budget.message_chars,
            system_chars=budget.system_chars,
            tool_schema_chars=budget.tool_schema_chars,
            message_limit_chars=budget.message_limit_chars,
            working_message_count=len(self.engine.messages),
            replacement_count=len(self.engine.content_replacements),
            compact_count=len(self.engine.session_store.load_compact_boundaries()),
        )

    async def compact(self) -> ContextStatus:
        async with self._session_lock:
            await self.engine.compact("manual")
            return self.context_status()

    def set_permission_handler(self, handler: PermissionHandler) -> None:
        self.permission_prompter.set_handler(handler)

    def providers(self) -> tuple[ProviderView, ...]:
        return self.provider_manager.list(self.settings.provider_id)

    async def configure_provider(self, update: ProviderUpdate) -> RuntimeStatus:
        connection = self.provider_manager.configure(update)
        await self.provider_router.switch(connection)
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
        return self.session_catalog.list(
            exclude_session_id=self.engine.session_store.session_id,
        )

    async def resume_session(self, session_id: str) -> ResumedSession:
        async with self._session_lock:
            if session_id == self.engine.session_store.session_id:
                raise ValueError("Session is already active")
            store = SessionStore(self.settings.paths.project_state_dir, session_id)
            messages = self.engine.resume(store)
            # 工具结果目录与 transcript 必须作为同一个 session-scoped 状态切换。
            self.engine.tool_executor.result_store = ToolResultStore(
                self.settings.paths.tool_results_dir(session_id)
            )
            return ResumedSession(
                status=self.status(),
                history=self._project_history(messages),
            )

    def _project_history(
        self, messages: tuple[ChatMessage, ...]
    ) -> tuple[HistoryEntry, ...]:
        """将内部消息投影为只用于 TUI 展示的稳定历史记录。"""

        results = {
            block.tool_use_id: block
            for message in messages
            for block in message.content
            if isinstance(block, ToolResultBlock)
        }
        history: list[HistoryEntry] = []
        for message in messages:
            if message.origin == "human":
                text = "\n".join(
                    block.text
                    for block in message.content
                    if isinstance(block, TextBlock)
                )
                if text:
                    history.append(HistoryUserMessage(text))
                continue
            if message.origin == "system":
                history.append(HistorySystemMessage("Conversation compacted"))
                continue
            if message.origin != "model":
                continue
            for block in message.content:
                if isinstance(block, TextBlock):
                    if block.text:
                        history.append(HistoryAssistantMessage(block.text))
                elif isinstance(block, ToolUseBlock):
                    call = ToolUseBlock(
                        block.id,
                        block.name,
                        to_json_object(block.input),
                    )
                    result = results.get(block.id)
                    result_presentation = (
                        self.engine.tool_executor.present_stored_result(call, result)
                    )
                    history.append(
                        HistoryToolCall(
                            tool_use_id=block.id,
                            use=self.engine.tool_executor.present_use(call),
                            result=result_presentation,
                            is_error=result is None or result.is_error,
                        )
                    )
        return tuple(history)
