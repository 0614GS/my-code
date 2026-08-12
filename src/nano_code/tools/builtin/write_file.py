"""Write complete text files within the workspace."""

from pathlib import Path

from nano_code.messages import JsonObject
from nano_code.tools.base import Tool, ToolContext, ToolDefinition, ToolOutput, ToolRisk
from nano_code.tools.paths import relative_display_path, resolve_workspace_path
from nano_code.tools.validation import required_string


class WriteFileTool(Tool):
    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="Write",
            description="Create or replace a UTF-8 text file in the workspace.",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
        )

    @property
    def risk(self) -> ToolRisk:
        return ToolRisk.WRITE

    def validate_input(self, tool_input: JsonObject) -> None:
        required_string(tool_input, "path")
        required_string(tool_input, "content", allow_empty=True)

    async def execute(self, tool_input: JsonObject, context: ToolContext) -> ToolOutput:
        path = resolve_workspace_path(
            context.cwd, required_string(tool_input, "path"), writable=True
        )
        content = required_string(tool_input, "content", allow_empty=True)
        self._write(path, content)
        display_path = relative_display_path(context.cwd, path)
        byte_count = len(content.encode("utf-8"))
        return ToolOutput(content=f"Wrote {byte_count} bytes to {display_path}")

    @staticmethod
    def _write(path: Path, content: str) -> None:
        if path.exists() and not path.is_file():
            raise IsADirectoryError(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
