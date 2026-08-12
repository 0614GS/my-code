"""Composition root for CLI dependencies."""

from uuid import uuid4

from nano_code.agent import AgentEngine
from nano_code.agent.prompt import build_system_prompt
from nano_code.config import Settings
from nano_code.context import ContextWindow
from nano_code.permissions import PermissionPolicy
from nano_code.permissions.prompt import HeadlessPrompter, TerminalPrompter
from nano_code.providers.anthropic import AnthropicProvider
from nano_code.sessions import SessionStore
from nano_code.tools import ToolContext, ToolRegistry
from nano_code.tools.builtin import builtin_tools
from nano_code.tools.executor import ToolExecutor
from nano_code.tools.result_store import ToolResultStore


def build_engine(settings: Settings, session_id: str | None = None) -> AgentEngine:
    """Wire concrete CLI adapters around the testable core."""

    actual_session_id = session_id or uuid4().hex
    session_store = SessionStore(settings.state_dir, actual_session_id)
    registry = ToolRegistry(builtin_tools())
    prompter = TerminalPrompter() if settings.interactive else HeadlessPrompter()
    tool_executor = ToolExecutor(
        registry=registry,
        policy=PermissionPolicy(mode=settings.permission_mode),
        prompter=prompter,
        context=ToolContext(cwd=settings.cwd),
        result_store=ToolResultStore(session_store.session_dir / "tool-results"),
    )
    provider = AnthropicProvider(
        model=settings.model,
        api_key=settings.api_key,
        base_url=settings.base_url,
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
