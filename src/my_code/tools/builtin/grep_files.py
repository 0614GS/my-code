"""使用正则表达式搜索文本文件。"""

import fnmatch
import re
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
from my_code.tools.validation import (
    optional_bool,
    optional_int,
    optional_string,
    required_string,
)

_MAX_SEARCH_FILE_BYTES = 2 * 1024 * 1024
_SKIPPED_ROOTS = frozenset({".git", ".my-code", ".venv", "__pycache__"})


class GrepTool(Tool):
    @property
    def definition(self) -> ModelToolDefinition:
        return ModelToolDefinition(
            name="Grep",
            description=(
                "Search UTF-8 workspace files with a Python regular expression."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {"type": "string", "default": "."},
                    "glob": {"type": "string", "default": "*"},
                    "case_sensitive": {"type": "boolean", "default": True},
                    "max_results": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 500,
                    },
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
        return f"Searching for {required_string(tool_input, 'pattern')}"

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
        pattern = required_string(tool_input, "pattern")
        self._compile(pattern, optional_bool(tool_input, "case_sensitive", True))
        optional_string(tool_input, "path", ".")
        optional_string(tool_input, "glob", "*")
        optional_int(tool_input, "max_results", 100, minimum=1, maximum=500)

    async def execute(
        self, tool_input: JsonObject, context: ToolExecutionContext
    ) -> ToolOutput:
        base = resolve_workspace_path(
            context.cwd,
            optional_string(tool_input, "path", "."),
            must_exist=True,
        )
        regex = self._compile(
            required_string(tool_input, "pattern"),
            optional_bool(tool_input, "case_sensitive", True),
        )
        file_glob = optional_string(tool_input, "glob", "*")
        limit = optional_int(tool_input, "max_results", 100, minimum=1, maximum=500)
        matches = self._grep(context.cwd, base, regex, file_glob, limit)
        return ToolOutput(
            content="\n".join(matches) if matches else "<no matches>",
            metadata={
                "match_count": len(matches),
                "first_match": matches[0] if matches else None,
            },
        )

    @staticmethod
    def _compile(pattern: str, case_sensitive: bool) -> re.Pattern[str]:
        try:
            flags = 0 if case_sensitive else re.IGNORECASE
            return re.compile(pattern, flags)
        except re.error as error:
            raise ToolInputError(f"Invalid regular expression: {error}") from error

    @staticmethod
    def _grep(
        cwd: Path,
        base: Path,
        regex: re.Pattern[str],
        file_glob: str,
        limit: int,
    ) -> list[str]:
        candidates = [base] if base.is_file() else base.rglob("*")
        matches: list[str] = []
        for path in candidates:
            if not path.is_file():
                continue
            relative = path.resolve().relative_to(cwd.resolve())
            if any(part in _SKIPPED_ROOTS for part in relative.parts):
                continue
            if not fnmatch.fnmatch(path.name, file_glob) and not fnmatch.fnmatch(
                relative.as_posix(), file_glob
            ):
                continue
            try:
                if path.stat().st_size > _MAX_SEARCH_FILE_BYTES:
                    continue
                raw = path.read_bytes()
            except OSError:
                continue
            if b"\x00" in raw:
                continue
            for line_number, line in enumerate(
                raw.decode("utf-8", errors="replace").splitlines(), start=1
            ):
                if regex.search(line):
                    display = relative_display_path(cwd, path)
                    matches.append(f"{display}:{line_number}:{line}")
                    if len(matches) >= limit:
                        return matches
        return matches
