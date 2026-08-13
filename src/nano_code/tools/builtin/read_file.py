"""读取工作区文件中有界的行范围。"""

from pathlib import Path

from nano_code.messages import JsonObject
from nano_code.tools.base import (
    Tool,
    ToolContext,
    ToolDefinition,
    ToolExecutionError,
    ToolOutput,
    ToolRisk,
)
from nano_code.tools.paths import relative_display_path, resolve_workspace_path
from nano_code.tools.validation import optional_int, required_string

_MAX_READ_BYTES = 8 * 1024 * 1024


class ReadFileTool(Tool):
    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
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
    def risk(self) -> ToolRisk:
        return ToolRisk.READ

    @property
    def concurrency_safe(self) -> bool:
        return True

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
        content = self._read(path, offset, limit)
        display_path = relative_display_path(context.cwd, path)
        return ToolOutput(content=f"{display_path}\n{content}")

    @staticmethod
    def _read(path: Path, offset: int, limit: int) -> str:
        if not path.is_file():
            raise ToolExecutionError(f"Not a file: {path}")
        if path.stat().st_size > _MAX_READ_BYTES:
            raise ToolExecutionError(
                f"File exceeds {_MAX_READ_BYTES // (1024 * 1024)} MiB read limit"
            )
        raw = path.read_bytes()
        if b"\x00" in raw:
            raise ToolExecutionError("Binary files are not supported by Read")
        lines = raw.decode("utf-8", errors="replace").splitlines()
        selected = lines[offset - 1 : offset - 1 + limit]
        if not selected:
            return "<no lines in requested range>"
        return "\n".join(
            f"{line_number:>6}\t{line}"
            for line_number, line in enumerate(selected, start=offset)
        )
