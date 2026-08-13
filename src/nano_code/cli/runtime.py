"""Composition root for CLI dependencies."""

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
from nano_code.agent.prompt import build_system_prompt
from nano_code.config import Settings
from nano_code.context import ContextWindow
from nano_code.messages import JsonObject
from nano_code.permissions import PermissionConfirmation, PermissionPolicy
from nano_code.permissions.models import PermissionDecision
from nano_code.permissions.prompt import (
    HeadlessPrompter,
    PermissionPrompter,
    TerminalPrompter,
)
from nano_code.providers.manager import ProviderManager, ProviderUpdate, ProviderView
from nano_code.providers.profiles import ProviderProtocol
from nano_code.providers.router import ProviderConnection, ProviderRouter
from nano_code.sessions import SessionStore
from nano_code.tools import Tool, ToolContext, ToolRegistry
from nano_code.tools.builtin import builtin_tools
from nano_code.tools.executor import ToolExecutor
from nano_code.tools.result_store import ToolResultStore
from nano_code.tui import (
    PermissionHandler,
    PermissionRequest,
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
    """Wire concrete CLI adapters around the testable core."""

    # Keep concrete SDK, terminal, and filesystem choices in this composition root.
    # The agent loop itself depends only on protocols and is testable with fakes.
    actual_session_id = session_id or str(uuid4())
    session_store = SessionStore(settings.paths.project_state_dir, actual_session_id)
    registry = ToolRegistry(builtin_tools())
    # Headless permission requests fail closed rather than assuming consent when
    # stdin/stdout cannot present a trustworthy confirmation prompt.
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
        context_window=ContextWindow(settings.context_chars),
        system_prompt=build_system_prompt(settings.cwd),
        max_turns=settings.max_turns,
        max_output_tokens=settings.max_output_tokens,
    )


class DeferredPermissionPrompter:
    """Bridge core permission checks to the active frontend without importing it."""

    def __init__(self) -> None:
        self._handler: PermissionHandler | None = None

    def set_handler(self, handler: PermissionHandler) -> None:
        self._handler = handler

    async def confirm(
        self, tool: Tool, tool_input: JsonObject, decision: PermissionDecision
    ) -> PermissionConfirmation:
        if self._handler is None:
            return PermissionConfirmation(False)
        return await self._handler(
            PermissionRequest(
                tool_name=tool.definition.name,
                tool_input=tool_input,
                message=decision.message,
            )
        )


class CliChatRuntime:
    """Adapt the concrete engine to the narrow contract consumed by the TUI."""

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

    async def submit(self, prompt: str) -> TurnResult:
        result = await self.engine.submit(prompt)
        return TurnResult(
            text=result.text,
            turns=result.turns,
            input_tokens=result.usage.input_tokens,
            output_tokens=result.usage.output_tokens,
        )

    async def stream(self, prompt: str) -> AsyncIterator[TurnEvent]:
        async for event in self.engine.submit_stream(prompt):
            if isinstance(event, AgentTextDelta):
                yield TextDelta(event.text)
            elif isinstance(event, AgentToolStarted):
                yield ToolStarted(event.tool_use_id, event.name, event.input)
            elif isinstance(event, AgentToolFinished):
                yield ToolFinished(
                    event.tool_use_id,
                    event.name,
                    event.content,
                    event.is_error,
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
