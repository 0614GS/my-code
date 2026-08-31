"""Minimal immutable context inputs and session-scoped runtime caches."""

from collections.abc import Callable
from dataclasses import dataclass, field

from my_code.context.documents import UserContextDocument
from my_code.conversation.models import ConversationEntry
from my_code.conversation.state import ContentReplacement
from my_code.model.primitives import ProviderReplayRecord
from my_code.model.request import ResolvedPromptSection, SystemPrompt
from my_code.prompts.registry import PromptRegistry


@dataclass(frozen=True, slots=True)
class ContextPlanningInput:
    context_entries: tuple[ConversationEntry, ...]
    content_replacements: tuple[ContentReplacement, ...] = ()
    replay_records: tuple[ProviderReplayRecord, ...] = ()


@dataclass(frozen=True, slots=True)
class AttachmentProjectionInput:
    session_id: str
    conversation: tuple[ConversationEntry, ...]
    context_entries: tuple[ConversationEntry, ...]


@dataclass(slots=True)
class SessionContextCache:
    """Non-persistent caches whose lifetime matches exactly one session or run."""

    _prompt_cache: dict[str, ResolvedPromptSection] = field(default_factory=dict)
    _user_context: tuple[UserContextDocument, ...] | None = None

    def resolve_prompt(self, registry: PromptRegistry) -> SystemPrompt:
        return registry.resolve(session_cache=self._prompt_cache)

    def user_context(
        self,
        resolve: Callable[[], tuple[UserContextDocument, ...]],
    ) -> tuple[UserContextDocument, ...]:
        if self._user_context is None:
            self._user_context = tuple(resolve())
        return self._user_context


__all__ = [
    "AttachmentProjectionInput",
    "ContextPlanningInput",
    "SessionContextCache",
]
