"""Safe full-transcript and request-audit projection."""

import hashlib
from dataclasses import fields, is_dataclass

from my_code.application.contracts.views import (
    TranscriptAttachment,
    TranscriptEntry,
    TranscriptField,
    TranscriptReasoning,
    TranscriptSummary,
    TranscriptText,
    TranscriptToolCall,
    TranscriptToolResult,
    TranscriptValue,
    TranscriptView,
)
from my_code.conversation.attachments import is_durable_attachment
from my_code.conversation.models import (
    AssistantMessage,
    AttachmentMessage,
    ConversationSummaryMessage,
    HumanMessage,
    ReasoningContent,
    TextContent,
    ToolCall,
    ToolResultBatch,
)
from my_code.model.primitives import ReasoningPresentation
from my_code.sessions.session import Session


def project_transcript(session: Session) -> TranscriptView:
    conversation = session.conversation
    tool_names = {
        block.id: block.name
        for message in conversation
        if isinstance(message, AssistantMessage)
        for block in message.content
        if isinstance(block, ToolCall)
    }
    entries: list[TranscriptEntry] = []
    for message in conversation:
        if isinstance(message, HumanMessage):
            entries.append(TranscriptText("user", message.content))
        elif isinstance(message, AssistantMessage):
            has_tools = any(isinstance(block, ToolCall) for block in message.content)
            for block in message.content:
                if isinstance(block, TextContent):
                    entries.append(
                        TranscriptText(
                            "assistant", block.text, is_final_answer=not has_tools
                        )
                    )
                elif isinstance(block, ReasoningContent):
                    presentation = block.presentation
                    if presentation.disclosure in {"hidden", "redacted"}:
                        presentation = ReasoningPresentation(
                            presentation.disclosure, ()
                        )
                    entries.append(TranscriptReasoning(presentation))
                else:
                    entries.append(
                        TranscriptToolCall(
                            block.name, freeze_transcript_value(block.input)
                        )
                    )
        elif isinstance(message, ToolResultBatch):
            entries.extend(
                TranscriptToolResult(
                    tool_names.get(result.tool_use_id, "Tool"),
                    result.content,
                    result.is_error,
                )
                for result in message.content
            )
        elif isinstance(message, ConversationSummaryMessage):
            entries.append(TranscriptSummary(message.content))
        elif isinstance(message, AttachmentMessage) and is_durable_attachment(
            message.payload
        ):
            entries.append(
                TranscriptAttachment(
                    message.payload.kind,
                    freeze_transcript_value(message.payload, omitted={"owner_run_id"}),
                )
            )
    audit = session.request_audit_snapshot()
    digest = hashlib.sha256(
        "\0".join(message.uuid for message in conversation).encode()
        + audit.revision.to_bytes(8, "big")
    ).digest()
    return TranscriptView(
        int.from_bytes(digest[:8], "big"),
        tuple(entries),
        audit.requests,
        audit.legacy_missing,
    )


def freeze_transcript_value(
    value: object, *, omitted: frozenset[str] | set[str] = frozenset()
) -> TranscriptValue:
    if is_dataclass(value) and not isinstance(value, type):
        return TranscriptValue(
            "object",
            fields=tuple(
                TranscriptField(
                    item.name,
                    freeze_transcript_value(getattr(value, item.name), omitted=omitted),
                )
                for item in fields(value)
                if item.name != "kind" and item.name not in omitted
            ),
        )
    if isinstance(value, dict):
        return TranscriptValue(
            "object",
            fields=tuple(
                TranscriptField(
                    str(key), freeze_transcript_value(item, omitted=omitted)
                )
                for key, item in value.items()
            ),
        )
    if isinstance(value, (list, tuple)):
        return TranscriptValue(
            "array",
            items=tuple(
                freeze_transcript_value(item, omitted=omitted) for item in value
            ),
        )
    scalar = (
        "null"
        if value is None
        else "true"
        if value is True
        else "false"
        if value is False
        else str(value)
    )
    return TranscriptValue("scalar", scalar=scalar)


__all__ = ["freeze_transcript_value", "project_transcript"]
