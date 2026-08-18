"""Frontend-neutral presentation values for chat applications."""

import json
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ToolUsePresentation:
    """Frontend-neutral semantics for displaying a tool invocation."""

    display_name: str
    summary: str
    activity: str


@dataclass(frozen=True, slots=True)
class ToolResultPresentation:
    """Frontend-neutral semantics for displaying a tool result."""

    summary: str
    detail: str | None = None
    truncated: bool = False


def compact_text(value: str, max_chars: int = 140) -> str:
    """Normalize arbitrary text into a bounded single-line summary."""

    normalized = " ".join(value.split())
    if len(normalized) <= max_chars:
        return normalized
    return f"{normalized[: max_chars - 1]}…"


def generic_tool_use_presentation(
    display_name: str, tool_input: object
) -> ToolUsePresentation:
    """Build a stable fallback projection for unknown or faulty tools."""

    try:
        serialized = json.dumps(tool_input, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        serialized = "<input unavailable>"
    return ToolUsePresentation(
        display_name=display_name,
        summary=compact_text(serialized),
        activity=f"Running {display_name}",
    )
