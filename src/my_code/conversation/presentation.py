"""Provider-neutral presentation values stored with conversation results."""

from dataclasses import dataclass
from typing import Literal

type FileDiffLineKind = Literal["context", "addition", "deletion", "omitted"]
MAX_FILE_DIFF_RECORDS = 200


@dataclass(frozen=True, slots=True)
class FileDiffLine:
    kind: FileDiffLineKind
    text: str
    old_line: int | None = None
    new_line: int | None = None
    omitted_lines: int = 0

    def __post_init__(self) -> None:
        if self.kind not in {"context", "addition", "deletion", "omitted"}:
            raise ValueError("Unsupported file diff line kind")
        if self.old_line is not None and self.old_line < 1:
            raise ValueError("Old line number must be positive")
        if self.new_line is not None and self.new_line < 1:
            raise ValueError("New line number must be positive")
        if self.kind == "context" and (self.old_line is None or self.new_line is None):
            raise ValueError("Context diff lines require both line numbers")
        if self.kind == "addition" and (
            self.old_line is not None or self.new_line is None
        ):
            raise ValueError("Added diff lines require only a new line number")
        if self.kind == "deletion" and (
            self.old_line is None or self.new_line is not None
        ):
            raise ValueError("Deleted diff lines require only an old line number")
        if self.kind == "omitted":
            if self.old_line is not None or self.new_line is not None:
                raise ValueError("Omitted diff lines cannot have line numbers")
            if self.omitted_lines < 1 or self.text:
                raise ValueError("Omitted diff lines require a positive count only")
        elif self.omitted_lines != 0:
            raise ValueError("Only omitted diff lines may carry an omitted count")


@dataclass(frozen=True, slots=True)
class FileDiffHunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: tuple[FileDiffLine, ...]

    def __post_init__(self) -> None:
        if self.old_start < 0 or self.new_start < 0:
            raise ValueError("Diff hunk starts must not be negative")
        if self.old_count < 0 or self.new_count < 0:
            raise ValueError("Diff hunk counts must not be negative")
        if not self.lines:
            raise ValueError("Diff hunks must contain lines")
        if not all(isinstance(line, FileDiffLine) for line in self.lines):
            raise TypeError("Diff hunks require file diff lines")


@dataclass(frozen=True, slots=True)
class FileDiffPresentation:
    path: str
    operation: Literal["created", "updated"]
    additions: int
    deletions: int
    hunks: tuple[FileDiffHunk, ...] = ()
    old_ends_with_newline: bool = False
    new_ends_with_newline: bool = False
    omitted_lines: int = 0
    omitted_reason: str | None = None

    def __post_init__(self) -> None:
        if not self.path.strip():
            raise ValueError("File diff path must not be empty")
        if self.operation not in {"created", "updated"}:
            raise ValueError("Unsupported file diff operation")
        if self.additions < 0 or self.deletions < 0 or self.omitted_lines < 0:
            raise ValueError("File diff counts must not be negative")
        if not all(isinstance(hunk, FileDiffHunk) for hunk in self.hunks):
            raise TypeError("File diffs require file diff hunks")
        if sum(len(hunk.lines) for hunk in self.hunks) > MAX_FILE_DIFF_RECORDS:
            raise ValueError("File diffs may contain at most 200 displayed records")
        if self.omitted_reason is not None and not self.omitted_reason.strip():
            raise ValueError("File diff omission reason must not be empty")


@dataclass(frozen=True, slots=True)
class ToolResultPresentation:
    summary: str
    detail: str | None = None
    truncated: bool = False
    file_diff: FileDiffPresentation | None = None

    def __post_init__(self) -> None:
        if not self.summary.strip():
            raise ValueError("Tool result presentation summary must not be empty")
        if self.file_diff is not None and not isinstance(
            self.file_diff, FileDiffPresentation
        ):
            raise TypeError("Tool result file diff must be a FileDiffPresentation")


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


__all__ = [
    "FileDiffHunk",
    "FileDiffLine",
    "FileDiffLineKind",
    "FileDiffPresentation",
    "MAX_FILE_DIFF_RECORDS",
    "ToolResultPresentation",
    "generic_tool_result_presentation",
]
