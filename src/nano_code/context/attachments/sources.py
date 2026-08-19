"""从当前会话快照派生 attachment。"""

import logging
from collections.abc import Callable, Iterable

from nano_code.context import ContextSnapshot
from nano_code.context.attachments.models import ContextAttachment

logger = logging.getLogger(__name__)

type DerivedAttachmentSource = Callable[[ContextSnapshot], Iterable[ContextAttachment]]


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


__all__ = ["DerivedAttachmentResolver", "DerivedAttachmentSource"]
