"""Find workspace paths with a glob pattern."""

from pathlib import Path

from nano_code.messages import JsonObject
from nano_code.tools.base import (
    Tool,
    ToolContext,
    ToolDefinition,
    ToolInputError,
    ToolOutput,
    ToolRisk,
)
from nano_code.tools.paths import relative_display_path, resolve_workspace_path
from nano_code.tools.validation import optional_int, optional_string, required_string


class GlobTool(Tool):
    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
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

    @property
    def risk(self) -> ToolRisk:
        return ToolRisk.READ

    @property
    def concurrency_safe(self) -> bool:
        return True

    def validate_input(self, tool_input: JsonObject) -> None:
        self._validate_pattern(required_string(tool_input, "pattern"))
        optional_string(tool_input, "path", ".")
        optional_int(tool_input, "limit", 200, minimum=1, maximum=500)

    async def execute(self, tool_input: JsonObject, context: ToolContext) -> ToolOutput:
        pattern = required_string(tool_input, "pattern")
        self._validate_pattern(pattern)
        base = resolve_workspace_path(
            context.cwd,
            optional_string(tool_input, "path", "."),
            must_exist=True,
        )
        limit = optional_int(tool_input, "limit", 200, minimum=1, maximum=500)
        matches = self._glob(context.cwd, base, pattern, limit)
        return ToolOutput(content="\n".join(matches) if matches else "<no matches>")

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
                if relative.parts and relative.parts[0] in {".git", ".nano-code"}:
                    continue
                matches.append(relative_display_path(cwd, resolved))
            except (OSError, ValueError):
                continue
        return sorted(set(matches))[:limit]
