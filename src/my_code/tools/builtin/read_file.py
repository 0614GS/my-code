"""读取工作区文件中有界的行范围。"""

from collections.abc import Callable
from pathlib import Path

from my_code.model.primitives import JsonObject
from my_code.model.request import ModelToolDefinition
from my_code.permissions.models import ToolPermissionContext, ToolPermissionResult
from my_code.tools.base import (
    Tool,
    ToolContext,
    ToolExecutionError,
    ToolOutput,
)
from my_code.tools.builtin.file_permissions import check_read_permission
from my_code.tools.paths import relative_display_path, resolve_workspace_path
from my_code.tools.presentation import ToolResultPresentation
from my_code.tools.validation import optional_int, required_string

_MAX_READ_BYTES = 8 * 1024 * 1024


class ReadFileTool(Tool):
    @property
    def definition(self) -> ModelToolDefinition:
        return ModelToolDefinition(
            name="Read",
            description="Read a UTF-8 text file from the current workspace.",
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
                    },
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        )

    @property
    def concurrency_safe(self) -> bool:
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
        path = resolve_workspace_path(
            context.cwd, required_string(tool_input, "path"), must_exist=True
        )
        offset = optional_int(tool_input, "offset", 1, minimum=1, maximum=10_000_000)
        limit = optional_int(tool_input, "limit", 2000, minimum=1, maximum=5000)
        content, truncated = self._read_details(
            path, offset, limit, read_bytes=context.workspace.read_bytes
        )
        display_path = relative_display_path(context.cwd, path)
        line_count = sum(1 for line in content.splitlines() if "\t" in line)
        return ToolOutput(
            content=f"{display_path}\n{content}",
            metadata={
                "path": display_path,
                "line_count": line_count,
                "truncated": truncated,
            },
        )

    @staticmethod
    def _read(path: Path, offset: int, limit: int) -> str:
        return ReadFileTool._read_details(path, offset, limit)[0]

    @staticmethod
    def _read_details(
        path: Path,
        offset: int,
        limit: int,
        *,
        read_bytes: Callable[[Path], bytes] = Path.read_bytes,
    ) -> tuple[str, bool]:
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
            return "<no lines in requested range>", False
        return (
            "\n".join(
                f"{line_number:>6}\t{line}"
                for line_number, line in enumerate(selected, start=offset)
            ),
            offset - 1 + len(selected) < len(lines),
        )
