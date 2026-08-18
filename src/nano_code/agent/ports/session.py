"""会话持久化使用的 outbound port。"""

from typing import Protocol, runtime_checkable

from nano_code.agent.contracts.session import (
    CompactBoundary,
    ContentReplacement,
    SessionSnapshot,
)
from nano_code.conversation import ConversationMessage


@runtime_checkable
class SessionRepository(Protocol):
    """ConversationState 用于恢复和追加会话记录的窄接口。"""

    @property
    def session_id(self) -> str: ...

    def load(self) -> SessionSnapshot: ...

    def append(self, message: ConversationMessage) -> bool: ...

    def append_content_replacement(self, replacement: ContentReplacement) -> bool: ...

    def append_compact_boundary(self, boundary: CompactBoundary) -> bool: ...


__all__ = ["SessionRepository"]
