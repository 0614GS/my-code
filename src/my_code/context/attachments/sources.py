"""从当前会话快照派生 attachment。"""

import logging
from collections.abc import Callable, Iterable
from typing import Protocol, runtime_checkable

from my_code.context.session_cache import AttachmentProjectionInput
from my_code.conversation.attachments import AttachmentPayload

logger = logging.getLogger(__name__)

type DerivedAttachmentSource = Callable[
    [AttachmentProjectionInput], Iterable[AttachmentPayload]
]


@runtime_checkable
class AttachmentObserver(Protocol):
    def acknowledge(self, attachments: tuple[AttachmentPayload, ...]) -> None: ...


class DerivedAttachmentResolver:
    """按声明顺序聚合同步的 snapshot-derived attachment source。"""

    def __init__(self, sources: Iterable[DerivedAttachmentSource] = ()) -> None:
        self._sources = tuple(sources)

    def resolve(
        self, state: AttachmentProjectionInput
    ) -> tuple[AttachmentPayload, ...]:
        """在不保留 resolver 内部状态的前提下解析一次快照。"""

        attachments: list[AttachmentPayload] = []
        for source in self._sources:
            try:
                source_attachments = tuple(source(state))
            except Exception:
                logger.exception("Attachment source failed; skipping it")
                continue
            attachments.extend(source_attachments)
        return tuple(attachments)

    def acknowledge(self, attachments: tuple[AttachmentPayload, ...]) -> None:
        """Notify stateful sources only after Session accepted attachments."""

        for source in self._sources:
            if not isinstance(source, AttachmentObserver):
                continue
            try:
                source.acknowledge(attachments)
            except Exception:
                logger.exception("Attachment delivery acknowledgement failed")


__all__ = [
    "DerivedAttachmentResolver",
    "DerivedAttachmentSource",
    "AttachmentObserver",
]
