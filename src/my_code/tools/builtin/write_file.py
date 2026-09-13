"""在工作区内写入完整文本文件。"""

from my_code.conversation.presentation import ToolResultPresentation
from my_code.foundation.json import JsonObject
from my_code.model.request import ModelToolDefinition
from my_code.permissions.models import ToolPermissionContext, ToolPermissionResult
from my_code.tools.base import (
    ConcurrencyAssessment,
    ConcurrencyMode,
    Tool,
    ToolExecutionContext,
    ToolExecutionError,
    ToolOutput,
    ToolResource,
)
from my_code.tools.builtin.file_diff import (
    build_file_diff,
    file_diff_from_json,
    file_diff_to_json,
)
from my_code.tools.builtin.file_permissions import check_write_permission
from my_code.tools.file_state import execution_session_key, text_line_count
from my_code.tools.paths import relative_display_path, resolve_workspace_path
from my_code.tools.validation import required_string
from my_code.workspace.local import WorkspaceConflictError


class WriteFileTool(Tool):
    def assess_concurrency(
        self, tool_input: JsonObject, context: ToolExecutionContext
    ) -> ConcurrencyAssessment:
        path = context.workspace.resolve(required_string(tool_input, "path"))
        return ConcurrencyAssessment(
            ConcurrencyMode.RESOURCE_SCOPED, (ToolResource(path, True),)
        )

    @property
    def definition(self) -> ModelToolDefinition:
        return ModelToolDefinition(
            name="Write",
            description=(
                "Create or replace a UTF-8 text file in the workspace. Before "
                "replacing an existing file, Read the entire current file."
            ),
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

    def get_tool_use_summary(self, tool_input: JsonObject) -> str:
        return required_string(tool_input, "path")

    def get_activity_description(self, tool_input: JsonObject) -> str:
        return f"Writing {required_string(tool_input, 'path')}"

    def is_read_only(
        self, tool_input: JsonObject, context: ToolExecutionContext
    ) -> bool:
        del tool_input, context
        return False

    async def check_permissions(
        self, tool_input: JsonObject, context: ToolPermissionContext
    ) -> ToolPermissionResult:
        return check_write_permission(
            self.definition.name, tool_input, context, must_exist=False
        )

    def present_result(
        self, tool_input: JsonObject, output: ToolOutput
    ) -> ToolResultPresentation:
        del tool_input
        path = output.metadata.get("path")
        byte_count = output.metadata.get("byte_count")
        if isinstance(path, str) and isinstance(byte_count, int):
            return ToolResultPresentation(
                summary=f"Wrote {byte_count} bytes to {path}",
                file_diff=file_diff_from_json(output.metadata.get("file_diff")),
            )
        return super().present_result({}, output)

    def validate_input(self, tool_input: JsonObject) -> None:
        required_string(tool_input, "path")
        required_string(tool_input, "content", allow_empty=True)

    async def execute(
        self, tool_input: JsonObject, context: ToolExecutionContext
    ) -> ToolOutput:
        path = resolve_workspace_path(
            context.cwd, required_string(tool_input, "path"), writable=True
        )
        content = required_string(tool_input, "content", allow_empty=True)
        session_key = execution_session_key(context.session_id, context.run_id)
        async with context.workspace.coordinator.path_lease(path, write=True):
            if path.exists() and not path.is_file():
                raise IsADirectoryError(path)
            created = not path.exists()
            expected = None
            if created:
                before = ""
            else:
                expected = context.file_reads.require_complete(session_key, path)
                if expected is None:
                    raise ToolExecutionError(
                        "Read the entire current file before replacing it"
                    )
                snapshot = context.workspace.read_snapshot(path)
                if snapshot.fingerprint != expected:
                    context.file_reads.invalidate(session_key, path)
                    raise ToolExecutionError("File changed since Read; Read it again")
                before = snapshot.content.decode("utf-8")
            try:
                written = context.workspace.atomic_write_text(
                    path,
                    content,
                    expected=expected,
                    must_not_exist=created,
                    create_parents=True,
                )
            except WorkspaceConflictError as error:
                context.file_reads.invalidate(session_key, path)
                raise ToolExecutionError(str(error)) from error
            context.file_reads.record_complete(
                session_key,
                path,
                written.fingerprint,
                total_lines=text_line_count(content),
            )
        display_path = relative_display_path(context.cwd, path)
        byte_count = len(content.encode("utf-8"))
        return ToolOutput(
            content=f"Wrote {byte_count} bytes to {display_path}",
            metadata={
                "path": display_path,
                "byte_count": byte_count,
                "file_diff": file_diff_to_json(
                    build_file_diff(display_path, before, content, created=created)
                ),
            },
        )
