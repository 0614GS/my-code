"""Frontend-neutral values for presenting tool activity."""

import json
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ToolUsePresentation:
    display_name: str
    summary: str
    activity: str


def compact_text(value: str, max_chars: int = 140) -> str:
    normalized = " ".join(value.split())
    if len(normalized) <= max_chars:
        return normalized
    return f"{normalized[: max_chars - 1]}…"


def generic_tool_use_presentation(
    display_name: str, tool_input: object
) -> ToolUsePresentation:
    try:
        serialized = json.dumps(tool_input, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        serialized = "<input unavailable>"
    return ToolUsePresentation(
        display_name=display_name,
        summary=compact_text(serialized),
        activity=f"Running {display_name}",
    )


__all__ = [
    "ToolUsePresentation",
    "compact_text",
    "generic_tool_use_presentation",
]
