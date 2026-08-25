"""从当前会话快照派生 attachment。"""

import logging
from collections.abc import Callable, Iterable
from typing import Protocol, runtime_checkable

from my_code.context.attachments.models import ContextAttachment
from my_code.context.session import AttachmentDelivery, ContextSnapshot

logger = logging.getLogger(__name__)

type DerivedAttachmentSource = Callable[[ContextSnapshot], Iterable[ContextAttachment]]


@runtime_checkable
class AttachmentDeliveryObserver(Protocol):
    def acknowledge(self, deliveries: tuple[AttachmentDelivery, ...]) -> None: ...


class DerivedAttachmentResolver:
    """按声明顺序聚合同步的 snapshot-derived attachment source。"""

    def __init__(self, sources: Iterable[DerivedAttachmentSource] = ()) -> None:
        self._sources = tuple(sources)

    def resolve(self, snapshot: ContextSnapshot) -> tuple[ContextAttachment, ...]:
        """在不保留 resolver 内部状态的前提下解析一次快照。"""

        attachments: list[ContextAttachment] = []
        for source in self._sources:
            try:
                source_attachments = tuple(source(snapshot))
            except Exception:
                logger.exception("Attachment source failed; skipping it")
                continue
            attachments.extend(source_attachments)
        return tuple(attachments)

    def acknowledge(self, deliveries: tuple[AttachmentDelivery, ...]) -> None:
        """Notify stateful sources only after Session accepted deliveries."""

        for source in self._sources:
            if not isinstance(source, AttachmentDeliveryObserver):
                continue
            try:
                source.acknowledge(deliveries)
            except Exception:
                logger.exception("Attachment delivery acknowledgement failed")


__all__ = [
    "DerivedAttachmentResolver",
    "DerivedAttachmentSource",
    "AttachmentDeliveryObserver",
]
