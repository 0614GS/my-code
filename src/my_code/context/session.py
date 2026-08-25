"""Immutable context views and the narrow Session context protocol."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from my_code.context.documents import UserContextDocument
from my_code.conversation.models import ConversationEntry
from my_code.conversation.state import ContentReplacement
from my_code.model.primitives import ProviderReplayRecord
from my_code.model.request import SystemPrompt
from my_code.prompts.registry import PromptRegistry


@dataclass(frozen=True, slots=True)
class ContextSnapshot:
    messages: tuple[ConversationEntry, ...]
    content_replacements: tuple[ContentReplacement, ...] = ()
    session_history: tuple[ConversationEntry, ...] = ()
    replay_records: tuple[ProviderReplayRecord, ...] = ()
    session_id: str | None = None


class SessionContextAccess(Protocol):
    """Operations backed by Session-owned, non-persistent context state."""

    def resolve_prompt(self, registry: PromptRegistry) -> SystemPrompt: ...

    def user_context(
        self,
        resolve: Callable[[], tuple[UserContextDocument, ...]],
    ) -> tuple[UserContextDocument, ...]: ...


__all__ = [
    "ContextSnapshot",
    "SessionContextAccess",
]
