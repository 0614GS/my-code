"""在工作区文件中执行精确字符串替换。"""

from pathlib import Path

from nano_code.agent.contracts.tool import ToolDefinition
from nano_code.messages import JsonObject
from nano_code.presentation import ToolResultPresentation
from nano_code.tools.base import (
    Tool,
    ToolContext,
    ToolExecutionError,
    ToolOutput,
    ToolRisk,
)
from nano_code.tools.paths import relative_display_path, resolve_workspace_path
from nano_code.tools.validation import optional_bool, required_string


class EditFileTool(Tool):
    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="Edit",
            description="Replace an exact string in an existing UTF-8 file.",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_string": {"type": "string"},
                    "new_string": {"type": "string"},
                    "replace_all": {"type": "boolean", "default": False},
                },
                "required": ["path", "old_string", "new_string"],
                "additionalProperties": False,
            },
        )

    @property
    def risk(self) -> ToolRisk:
        return ToolRisk.WRITE

    def get_tool_use_summary(self, tool_input: JsonObject) -> str:
        return required_string(tool_input, "path")

    def get_activity_description(self, tool_input: JsonObject) -> str:
        return f"Editing {required_string(tool_input, 'path')}"

    def present_result(
        self, tool_input: JsonObject, output: ToolOutput
    ) -> ToolResultPresentation:
        del tool_input
        path = output.metadata.get("path")
        replacements = output.metadata.get("replacements")
        if isinstance(path, str) and isinstance(replacements, int):
            return ToolResultPresentation(
                summary=f"Replaced {replacements} occurrence(s) in {path}"
            )
        return super().present_result({}, output)

    def validate_input(self, tool_input: JsonObject) -> None:
        required_string(tool_input, "path")
        required_string(tool_input, "old_string")
        required_string(tool_input, "new_string", allow_empty=True)
        optional_bool(tool_input, "replace_all", False)

    async def execute(self, tool_input: JsonObject, context: ToolContext) -> ToolOutput:
        path = resolve_workspace_path(
            context.cwd,
            required_string(tool_input, "path"),
            must_exist=True,
            writable=True,
        )
        old = required_string(tool_input, "old_string")
        new = required_string(tool_input, "new_string", allow_empty=True)
        replace_all = optional_bool(tool_input, "replace_all", False)
        replacements = self._edit(path, old, new, replace_all)
        display_path = relative_display_path(context.cwd, path)
        return ToolOutput(
            content=f"Replaced {replacements} occurrence(s) in {display_path}",
            metadata={"path": display_path, "replacements": replacements},
        )

    @staticmethod
    def _edit(path: Path, old: str, new: str, replace_all: bool) -> int:
        if not path.is_file():
            raise ToolExecutionError(f"Not a file: {path}")
        content = path.read_text(encoding="utf-8")
        count = content.count(old)
        if count == 0:
            raise ToolExecutionError("old_string was not found")
        if not replace_all and count != 1:
            raise ToolExecutionError(
                f"old_string occurs {count} times; set replace_all "
                "or provide more context"
            )
        limit = -1 if replace_all else 1
        path.write_text(content.replace(old, new, limit), encoding="utf-8")
        return count if replace_all else 1
