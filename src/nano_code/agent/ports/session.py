"""会话持久化使用的 outbound port。"""

from typing import Protocol, runtime_checkable

from nano_code.agent.contracts.session import (
    CompactBoundary,
    ContentReplacement,
    SessionSnapshot,
)
from nano_code.messages import TranscriptMessage


@runtime_checkable
class SessionRepository(Protocol):
    """ConversationState 需要的追加式会话事实来源。"""

    @property
    def session_id(self) -> str: ...

    def snapshot(self) -> SessionSnapshot: ...

    def append(self, message: TranscriptMessage) -> None: ...

    def append_content_replacement(self, replacement: ContentReplacement) -> None: ...

    def append_compact_boundary(self, boundary: CompactBoundary) -> None: ...


__all__ = ["SessionRepository"]
