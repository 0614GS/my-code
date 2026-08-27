"""读取工作区文件中有界的行范围。"""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from my_code.conversation.presentation import ToolResultPresentation
from my_code.foundation.json import JsonObject
from my_code.model.request import ModelToolDefinition
from my_code.permissions.models import ToolPermissionContext, ToolPermissionResult
from my_code.tools.base import (
    Tool,
    ToolContext,
    ToolExecutionError,
    ToolOutput,
)
from my_code.tools.builtin.file_permissions import check_read_permission
from my_code.tools.paths import relative_display_path, resolve_read_path
from my_code.tools.validation import optional_int, required_string

_MAX_READ_BYTES = 8 * 1024 * 1024
_MAX_OUTPUT_CHARS = 16_000


@dataclass(frozen=True, slots=True)
class _ReadDetails:
    content: str
    returned_start: int | None
    returned_end: int | None
    total_lines: int
    next_offset: int | None
    truncated_by: str | None


class ReadFileTool(Tool):
    @property
    def definition(self) -> ModelToolDefinition:
        return ModelToolDefinition(
            name="Read",
            description=(
                "Read a bounded UTF-8 line range from the current workspace. "
                "The default is 2,000 lines. For large files, prefer a targeted "
                "offset/limit or Grep and continue from next_offset when returned."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path"},
                    "offset": {
                        "type": "integer",
                        "minimum": 1,
                        "description": "First 1-based line to return",
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 5000,
                        "description": "Maximum lines to return (default 2000)",
                    },
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        )

    def is_concurrency_safe(self, tool_input: JsonObject) -> bool:
        del tool_input
        return True

    def get_tool_use_summary(self, tool_input: JsonObject) -> str:
        return required_string(tool_input, "path")

    def get_activity_description(self, tool_input: JsonObject) -> str:
        return f"Reading {required_string(tool_input, 'path')}"

    def is_read_only(self, tool_input: JsonObject, context: ToolContext) -> bool:
        del tool_input, context
        return True

    async def check_permissions(
        self, tool_input: JsonObject, context: ToolPermissionContext
    ) -> ToolPermissionResult:
        return check_read_permission(self.definition.name, tool_input, context)

    def present_result(
        self, tool_input: JsonObject, output: ToolOutput
    ) -> ToolResultPresentation:
        del tool_input
        path = output.metadata.get("path")
        line_count = output.metadata.get("line_count")
        if isinstance(path, str) and isinstance(line_count, int):
            return ToolResultPresentation(
                summary=f"Read {line_count} line(s) from {path}"
            )
        return super().present_result({}, output)

    def validate_input(self, tool_input: JsonObject) -> None:
        required_string(tool_input, "path")
        optional_int(tool_input, "offset", 1, minimum=1, maximum=10_000_000)
        optional_int(tool_input, "limit", 2000, minimum=1, maximum=5000)

    async def execute(self, tool_input: JsonObject, context: ToolContext) -> ToolOutput:
        path, internal = resolve_read_path(
            context.cwd,
            required_string(tool_input, "path"),
            internal_root=context.internal_read_root,
            must_exist=True,
        )
        offset = optional_int(tool_input, "offset", 1, minimum=1, maximum=10_000_000)
        limit = optional_int(tool_input, "limit", 2000, minimum=1, maximum=5000)
        display_path = (
            str(path) if internal else relative_display_path(context.cwd, path)
        )
        details = self._read_details(
            path,
            offset,
            limit,
            max_chars=max(1, _MAX_OUTPUT_CHARS - len(display_path) - 300),
            read_bytes=Path.read_bytes if internal else context.workspace.read_bytes,
        )
        if details.returned_start is None:
            range_text = "no lines returned"
        else:
            range_text = (
                f"lines {details.returned_start}-{details.returned_end} "
                f"of {details.total_lines}"
            )
        continuation = (
            f"; next_offset={details.next_offset}"
            if details.next_offset is not None
            else ""
        )
        truncation = (
            f"; truncated_by={details.truncated_by}"
            if details.truncated_by is not None
            else ""
        )
        content = (
            f"{display_path}\n[{range_text}{continuation}{truncation}]\n"
            f"{details.content}"
        )
        content = content[:_MAX_OUTPUT_CHARS]
        line_count = (
            0
            if details.returned_start is None or details.returned_end is None
            else details.returned_end - details.returned_start + 1
        )
        return ToolOutput(
            content=content,
            metadata={
                "path": display_path,
                "line_count": line_count,
                "truncated": details.truncated_by is not None,
                "returned_start": details.returned_start,
                "returned_end": details.returned_end,
                "total_lines": details.total_lines,
                "next_offset": details.next_offset,
                "truncated_by": details.truncated_by,
            },
        )

    @staticmethod
    def _read(path: Path, offset: int, limit: int) -> str:
        return ReadFileTool._read_details(path, offset, limit).content

    @staticmethod
    def _read_details(
        path: Path,
        offset: int,
        limit: int,
        *,
        max_chars: int = _MAX_OUTPUT_CHARS,
        read_bytes: Callable[[Path], bytes] = Path.read_bytes,
    ) -> _ReadDetails:
        if not path.is_file():
            raise ToolExecutionError(f"Not a file: {path}")
        if path.stat().st_size > _MAX_READ_BYTES:
            raise ToolExecutionError(
                f"File exceeds {_MAX_READ_BYTES // (1024 * 1024)} MiB read limit"
            )
        raw = read_bytes(path)
        if b"\x00" in raw:
            raise ToolExecutionError("Binary files are not supported by Read")
        try:
            lines = raw.decode("utf-8").splitlines()
        except UnicodeDecodeError as error:
            raise ToolExecutionError("File is not valid UTF-8 text") from error
        selected = lines[offset - 1 : offset - 1 + limit]
        if not selected:
            return _ReadDetails(
                "<no lines in requested range>", None, None, len(lines), None, None
            )

        rendered: list[str] = []
        used = 0
        truncated_by: str | None = None
        for line_number, line in enumerate(selected, start=offset):
            formatted = f"{line_number:>6}\t{line}"
            separator = 1 if rendered else 0
            if used + separator + len(formatted) <= max_chars:
                rendered.append(formatted)
                used += separator + len(formatted)
                continue
            if not rendered:
                rendered.append(formatted[:max_chars])
                truncated_by = "line_chars"
            else:
                truncated_by = "characters"
            break

        returned_end = offset + len(rendered) - 1
        limited_end = offset - 1 + len(selected)
        if truncated_by == "line_chars":
            next_offset = None
        elif returned_end < len(lines):
            next_offset = returned_end + 1
            truncated_by = truncated_by or (
                "line_limit" if limited_end < len(lines) else "characters"
            )
        else:
            next_offset = None
        return _ReadDetails(
            "\n".join(rendered),
            offset,
            returned_end,
            len(lines),
            next_offset,
            truncated_by,
        )
