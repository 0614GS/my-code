"""Conversation attachment source for the current Skill catalog."""

from typing import Protocol

from my_code.context.session import AttachmentDerivationState
from my_code.conversation.attachments import SkillListingAttachment, SkillListingEntry
from my_code.conversation.models import AttachmentMessage
from my_code.skills.catalog import SkillCatalogSnapshot


class SkillSnapshotSource(Protocol):
    def snapshot(self) -> SkillCatalogSnapshot: ...


class SkillListingAttachmentSource:
    def __init__(self, catalog: SkillSnapshotSource) -> None:
        self._catalog = catalog

    def __call__(
        self, state: AttachmentDerivationState
    ) -> tuple[SkillListingAttachment, ...]:
        catalog = self._catalog.snapshot()
        if not catalog.entries:
            return ()
        already_listed = any(
            isinstance(message, AttachmentMessage)
            and isinstance(message.payload, SkillListingAttachment)
            and message.payload.catalog_version == catalog.version
            for message in state.context_entries
        )
        if already_listed:
            return ()
        return (
            SkillListingAttachment(
                catalog.version,
                tuple(
                    SkillListingEntry(entry.name, entry.description, str(entry.source))
                    for entry in catalog.entries
                ),
            ),
        )


__all__ = ["SkillListingAttachmentSource"]
