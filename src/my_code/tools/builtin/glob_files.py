"""使用 glob 模式查找工作区路径。"""

from pathlib import Path

from my_code.conversation.presentation import ToolResultPresentation
from my_code.foundation.json import JsonObject
from my_code.model.request import ModelToolDefinition
from my_code.permissions.models import ToolPermissionContext, ToolPermissionResult
from my_code.tools.base import (
    Tool,
    ToolExecutionContext,
    ToolInputError,
    ToolOutput,
)
from my_code.tools.builtin.file_permissions import check_read_permission
from my_code.tools.paths import relative_display_path, resolve_workspace_path
from my_code.tools.presentation import compact_text
from my_code.tools.validation import optional_int, optional_string, required_string


class GlobTool(Tool):
    @property
    def definition(self) -> ModelToolDefinition:
        return ModelToolDefinition(
            name="Glob",
            description="Find files and directories using a workspace-relative glob.",
            input_schema={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "e.g. **/*.py"},
                    "path": {"type": "string", "default": "."},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 500},
                },
                "required": ["pattern"],
                "additionalProperties": False,
            },
        )

    def is_concurrency_safe(self, tool_input: JsonObject) -> bool:
        del tool_input
        return True

    def get_tool_use_summary(self, tool_input: JsonObject) -> str:
        pattern = required_string(tool_input, "pattern")
        path = optional_string(tool_input, "path", ".")
        return pattern if path == "." else f"{pattern} · {path}"

    def get_activity_description(self, tool_input: JsonObject) -> str:
        return f"Finding {required_string(tool_input, 'pattern')}"

    def is_read_only(
        self, tool_input: JsonObject, context: ToolExecutionContext
    ) -> bool:
        del tool_input, context
        return True

    async def check_permissions(
        self, tool_input: JsonObject, context: ToolPermissionContext
    ) -> ToolPermissionResult:
        return check_read_permission(
            self.definition.name, tool_input, context, path_key="path"
        )

    def present_result(
        self, tool_input: JsonObject, output: ToolOutput
    ) -> ToolResultPresentation:
        del tool_input
        count = output.metadata.get("match_count")
        first = output.metadata.get("first_match")
        if count == 0:
            return ToolResultPresentation(summary="no matches")
        if isinstance(count, int) and isinstance(first, str):
            return ToolResultPresentation(
                summary=f"{count} match(es) · {compact_text(first)}",
                truncated=count > 1,
            )
        return super().present_result({}, output)

    def validate_input(self, tool_input: JsonObject) -> None:
        self._validate_pattern(required_string(tool_input, "pattern"))
        optional_string(tool_input, "path", ".")
        optional_int(tool_input, "limit", 200, minimum=1, maximum=500)

    async def execute(
        self, tool_input: JsonObject, context: ToolExecutionContext
    ) -> ToolOutput:
        pattern = required_string(tool_input, "pattern")
        self._validate_pattern(pattern)
        base = resolve_workspace_path(
            context.cwd,
            optional_string(tool_input, "path", "."),
            must_exist=True,
        )
        limit = optional_int(tool_input, "limit", 200, minimum=1, maximum=500)
        bounded = self._glob(context.cwd, base, pattern, limit + 1)
        truncated = len(bounded) > limit
        matches = bounded[:limit]
        return ToolOutput(
            content="\n".join(matches) if matches else "<no matches>",
            metadata={
                "match_count": len(matches),
                "first_match": matches[0] if matches else None,
                "truncated": truncated,
            },
        )

    @staticmethod
    def _validate_pattern(pattern: str) -> None:
        path = Path(pattern)
        if path.is_absolute() or ".." in path.parts:
            raise ToolInputError("Glob pattern must stay within its base path")

    @staticmethod
    def _glob(cwd: Path, base: Path, pattern: str, limit: int) -> list[str]:
        matches: list[str] = []
        for candidate in base.glob(pattern):
            try:
                resolved = candidate.resolve()
                if not resolved.is_relative_to(cwd.resolve()):
                    continue
                relative = resolved.relative_to(cwd.resolve())
                if relative.parts and relative.parts[0] in {".git", ".my-code"}:
                    continue
                matches.append(relative_display_path(cwd, resolved))
            except (OSError, ValueError):
                continue
        return sorted(set(matches))[:limit]
