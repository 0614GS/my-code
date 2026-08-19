"""Safe status snapshots owned by the chat use case."""

from dataclasses import dataclass

from nano_code.features.todos.models import TodoItem


@dataclass(frozen=True, slots=True)
class RuntimeStatus:
    session_id: str
    cwd: str
    provider_id: str
    base_url: str | None
    model: str
    permission_mode: str
    credential_source: str
    working_message_count: int
    todos: tuple[TodoItem, ...]


@dataclass(frozen=True, slots=True)
class ContextStatus:
    estimated_input_tokens: int
    reserved_output_tokens: int
    estimated_total_tokens: int
    message_chars: int
    system_chars: int
    tool_schema_chars: int
    message_limit_chars: int
    working_message_count: int
    replacement_count: int
    compact_count: int
    user_context_chars: int = 0
    attachment_chars: int = 0
    input_tokens: int = 0
    input_limit_tokens: int = 200_000
    compact_trigger_tokens: int = 180_000
    remaining_input_tokens: int = 0
    measurement: str = "tokenizer_estimate"
    model_limit_source: str = "fallback"
    configured_compact_trigger_tokens: int | None = None
    warning: str | None = None


__all__ = [
    "ContextStatus",
    "RuntimeStatus",
]
