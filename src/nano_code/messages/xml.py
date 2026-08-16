"""Render trusted context blocks as model-visible XML markers."""

from typing import Literal

from nano_code.messages.models import SystemContextBlock, SystemContextKind

type XmlTag = Literal["system-reminder", "conversation-summary"]

_TAGS: dict[SystemContextKind, XmlTag] = {
    "system_reminder": "system-reminder",
    "conversation_summary": "conversation-summary",
}


def wrap_xml(tag: XmlTag, content: str) -> str:
    """Wrap trusted content in a known XML marker.

    XML markers are a model-facing text protocol, not a security boundary.
    Escaping the matching closing tag prevents trusted content from closing the
    wrapper early while preserving the existing content representation.
    """

    escaped = content.replace(f"</{tag}>", f"&lt;/{tag}&gt;")
    return f"<{tag}>\n{escaped}\n</{tag}>"


def render_system_context(block: SystemContextBlock) -> str:
    """Render a structured system context block for a model request."""

    return wrap_xml(_TAGS[block.kind], block.content)


__all__ = ["render_system_context", "wrap_xml"]
