"""Frontend-neutral values for presenting tool activity."""

import json
from dataclasses import dataclass
from typing import Literal

type ToolDisplayCategory = Literal["explore", "command", "change", "other"]


@dataclass(frozen=True, slots=True)
class ToolUsePresentation:
    display_name: str
    summary: str
    activity: str
    category: ToolDisplayCategory = "other"


def tool_display_category(tool_name: str) -> ToolDisplayCategory:
    if tool_name in {"Read", "Glob", "Grep", "ToolSearch"}:
        return "explore"
    if tool_name == "Bash":
        return "command"
    if tool_name in {"Edit", "Write"}:
        return "change"
    return "other"


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
        category=tool_display_category(display_name),
    )


__all__ = [
    "ToolUsePresentation",
    "ToolDisplayCategory",
    "compact_text",
    "generic_tool_use_presentation",
    "tool_display_category",
]
