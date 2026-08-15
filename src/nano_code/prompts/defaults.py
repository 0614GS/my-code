"""nano-code 默认提示词片段。"""

from pathlib import Path

from nano_code.prompts.models import PromptSection, PromptStability
from nano_code.prompts.registry import PromptRegistry

_IDENTITY = """You are nano-code, a concise coding agent.
Inspect relevant files before changing them, and base decisions on observed facts."""

_TOOL_USE = """Use the available tools to inspect and modify the workspace.
Prefer small, focused changes and run verification proportionate to the risk."""

_SAFETY = """Keep file operations within the workspace.
Never modify .git, .nano-code."""

_RESPONSE_STYLE = """Report outcomes clearly and mention unresolved failures.
Avoid repeating tool output when a concise explanation is sufficient."""


def _literal(content: str) -> str:
    return content


def default_prompt_registry(cwd: Path) -> PromptRegistry:
    """构造默认片段；路径只进入 session 稳定区。"""

    workspace = str(cwd)
    return PromptRegistry(
        (
            PromptSection(
                "nano-code.identity",
                PromptStability.STATIC,
                lambda: _literal(_IDENTITY),
            ),
            PromptSection(
                "nano-code.tools",
                PromptStability.STATIC,
                lambda: _literal(_TOOL_USE),
            ),
            PromptSection(
                "nano-code.safety",
                PromptStability.STATIC,
                lambda: _literal(_SAFETY),
            ),
            PromptSection(
                "nano-code.response-style",
                PromptStability.STATIC,
                lambda: _literal(_RESPONSE_STYLE),
            ),
            PromptSection(
                "nano-code.environment",
                PromptStability.SESSION,
                lambda: f"The current workspace is {workspace}.",
            ),
        )
    )
