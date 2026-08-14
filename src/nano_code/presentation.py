"""核心层与前端之间共享的工具展示值对象。"""

import json
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ToolUsePresentation:
    """工具调用的前端无关展示语义。"""

    display_name: str
    summary: str
    activity: str


@dataclass(frozen=True, slots=True)
class ToolResultPresentation:
    """工具结果的前端无关展示语义。"""

    summary: str
    detail: str | None = None
    truncated: bool = False


def compact_text(value: str, max_chars: int = 140) -> str:
    """把任意文本规范化为单行有界展示。"""

    normalized = " ".join(value.split())
    if len(normalized) <= max_chars:
        return normalized
    return f"{normalized[: max_chars - 1]}…"


def generic_tool_use_presentation(
    display_name: str, tool_input: object
) -> ToolUsePresentation:
    """为未知工具或故障展示扩展生成稳定的通用投影。"""

    try:
        serialized = json.dumps(tool_input, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        serialized = "<input unavailable>"
    return ToolUsePresentation(
        display_name=display_name,
        summary=compact_text(serialized),
        activity=f"Running {display_name}",
    )
