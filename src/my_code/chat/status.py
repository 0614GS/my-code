"""Safe status snapshots owned by the chat use case."""

from dataclasses import dataclass

from my_code.features.todos.models import TodoItem


@dataclass(frozen=True, slots=True)
class RuntimeStatus:
    session_id: str
    cwd: str
    provider_id: str
    base_url: str | None
    model: str
    permission_mode: str
    credential_source: str
    context_entry_count: int
    conversation_entry_count: int
    todos: tuple[TodoItem, ...]
    tool_count: int = 0
    skill_count: int = 0
    mcp_connected_count: int = 0
    mcp_server_count: int = 0
    execution_environment: str = "local"
    collaboration_mode: str = "default"


@dataclass(frozen=True, slots=True)
class ContextStatus:
    reported_base_tokens: int | None
    estimated_delta_tokens: int
    projected_tokens: int
    reserved_output_tokens: int
    context_entry_count: int
    conversation_entry_count: int
    replacement_count: int
    compact_count: int
    input_limit_tokens: int = 200_000
    compact_trigger_tokens: int = 180_000
    remaining_input_tokens: int = 0
    measurement: str = "estimated"
    model_limit_source: str = "fallback"
    configured_compact_trigger_tokens: int | None = None
    warning: str | None = None


__all__ = [
    "ContextStatus",
    "RuntimeStatus",
]
