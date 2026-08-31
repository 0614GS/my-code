"""Append-only collaboration world-state derivation at input boundaries."""

from my_code.conversation.attachments import (
    AttachmentPayload,
    CollaborationModeAttachment,
    ToolDiscoveryAttachment,
    ToolDiscoveryInvalidationAttachment,
)
from my_code.conversation.models import AttachmentMessage, ConversationEntry
from my_code.sessions.models import CollaborationMode
from my_code.tools.catalog import ToolCatalogSnapshot
from my_code.tools.discovery import discovery_definition, restored_discoveries

QUESTION_TOOL_NAME = "Question"


def resolve_mode_prelude(
    *,
    mode: CollaborationMode,
    context_entries: tuple[ConversationEntry, ...],
    catalog: ToolCatalogSnapshot,
    discovery_mode: str,
) -> tuple[AttachmentPayload, ...]:
    """Return only collaboration state missing from the effective context."""

    current = _latest_context_mode(context_entries)
    discovered = restored_discoveries(context_entries)
    result: list[AttachmentPayload] = []
    if current is not mode and not (
        current is None and mode is CollaborationMode.DEFAULT
    ):
        result.append(CollaborationModeAttachment(mode.value))
    question = catalog.get(QUESTION_TOOL_NAME)
    if mode is CollaborationMode.PLAN and question is not None:
        definition = discovery_definition(question)
        if discovered.get(QUESTION_TOOL_NAME) != definition:
            result.append(
                ToolDiscoveryAttachment(
                    (definition,),
                    "native" if discovery_mode == "native" else "dispatcher",
                )
            )
    elif QUESTION_TOOL_NAME in discovered:
        result.append(ToolDiscoveryInvalidationAttachment((QUESTION_TOOL_NAME,)))
    return tuple(result)


def _latest_context_mode(
    entries: tuple[ConversationEntry, ...],
) -> CollaborationMode | None:
    for entry in reversed(entries):
        if isinstance(entry, AttachmentMessage) and isinstance(
            entry.payload, CollaborationModeAttachment
        ):
            return CollaborationMode(entry.payload.mode)
    return None


__all__ = ["QUESTION_TOOL_NAME", "resolve_mode_prelude"]
