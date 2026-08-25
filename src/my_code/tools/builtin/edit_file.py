"""在工作区文件中执行精确字符串替换。"""

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
from my_code.tools.builtin.file_permissions import check_write_permission
from my_code.tools.paths import relative_display_path, resolve_workspace_path
from my_code.tools.validation import optional_bool, required_string


class EditFileTool(Tool):
    @property
    def definition(self) -> ModelToolDefinition:
        return ModelToolDefinition(
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

    def get_tool_use_summary(self, tool_input: JsonObject) -> str:
        return required_string(tool_input, "path")

    def get_activity_description(self, tool_input: JsonObject) -> str:
        return f"Editing {required_string(tool_input, 'path')}"

    def is_read_only(self, tool_input: JsonObject, context: ToolContext) -> bool:
        del tool_input, context
        return False

    async def check_permissions(
        self, tool_input: JsonObject, context: ToolPermissionContext
    ) -> ToolPermissionResult:
        return check_write_permission(
            self.definition.name, tool_input, context, must_exist=True
        )

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
        if not path.is_file():
            raise ToolExecutionError(f"Not a file: {path}")
        content = context.workspace.read_text(path)
        count = content.count(old)
        if count == 0:
            raise ToolExecutionError("old_string was not found")
        if not replace_all and count != 1:
            raise ToolExecutionError(
                f"old_string occurs {count} times; set replace_all "
                "or provide more context"
            )
        limit = -1 if replace_all else 1
        context.workspace.write_text(path, content.replace(old, new, limit))
        replacements = count if replace_all else 1
        display_path = relative_display_path(context.cwd, path)
        return ToolOutput(
            content=f"Replaced {replacements} occurrence(s) in {display_path}",
            metadata={"path": display_path, "replacements": replacements},
        )
