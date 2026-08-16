"""Request-scoped attachment resolution."""

import logging
from collections.abc import Callable, Iterable

from nano_code.agent.contracts.session import ConversationSnapshot
from nano_code.messages import AttachmentMessage

logger = logging.getLogger(__name__)

type AttachmentSource = Callable[
    [ConversationSnapshot], Iterable[AttachmentMessage]
]


class AttachmentResolver:
    """Aggregate request-scoped attachments from ordered synchronous sources."""

    def __init__(self, sources: Iterable[AttachmentSource] = ()) -> None:
        self._sources = tuple(sources)

    def resolve(
        self, snapshot: ConversationSnapshot
    ) -> tuple[AttachmentMessage, ...]:
        """Resolve all sources for one snapshot without retaining request state."""

        attachments: list[AttachmentMessage] = []
        for source in self._sources:
            try:
                source_attachments = tuple(source(snapshot))
            except Exception:
                logger.exception("Attachment source failed; skipping it")
                continue
            attachments.extend(source_attachments)
        return tuple(attachments)


__all__ = ["AttachmentResolver", "AttachmentSource"]
