"""Provider-neutral presentation values stored with conversation results."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ToolResultPresentation:
    summary: str
    detail: str | None = None
    truncated: bool = False

    def __post_init__(self) -> None:
        if not self.summary.strip():
            raise ValueError("Tool result presentation summary must not be empty")


def generic_tool_result_presentation(
    content: str,
    is_error: bool,
    *,
    max_chars: int = 140,
) -> ToolResultPresentation:
    """Create a stable fallback without consulting the current tool catalog."""

    first_line = next(
        (line.strip() for line in content.splitlines() if line.strip()), ""
    )
    if not first_line:
        first_line = (
            "Tool failed without details"
            if is_error
            else "Tool completed without output"
        )
    truncated = len(first_line) > max_chars
    summary = first_line if not truncated else f"{first_line[: max_chars - 1]}…"
    return ToolResultPresentation(summary=summary, truncated=truncated)


__all__ = ["ToolResultPresentation", "generic_tool_result_presentation"]
