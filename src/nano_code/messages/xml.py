"""Render trusted context blocks as model-visible XML markers."""

from typing import Literal

from nano_code.messages.context import ContextInstruction

type XmlTag = Literal["system-reminder", "conversation-summary"]


def wrap_xml(tag: XmlTag, content: str) -> str:
    """Wrap trusted content in a known XML marker.

    XML markers are a model-facing text protocol, not a security boundary.
    Escaping the matching closing tag prevents trusted content from closing the
    wrapper early while preserving the existing content representation.
    """

    escaped = content.replace(f"</{tag}>", f"&lt;/{tag}&gt;")
    return f"<{tag}>\n{escaped}\n</{tag}>"


def render_context_instruction(block: ContextInstruction) -> str:
    return wrap_xml("system-reminder", block.content)


__all__ = ["render_context_instruction", "wrap_xml"]
