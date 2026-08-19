"""System-prompt content and its request-lifetime section assembly."""

import os
import platform
import sys
from pathlib import Path

from nano_code.constants.prompts import (
    IDENTITY_PROMPT,
    RESPONSE_STYLE_PROMPT,
    SAFETY_PROMPT,
    SYSTEM_PROMPT,
    TASK_GUIDANCE_PROMPT,
    TOOLS_PROMPT,
)
from nano_code.model import PromptStability
from nano_code.prompts.models import PromptSection
from nano_code.prompts.registry import PromptRegistry


def _environment_prompt(cwd: Path) -> str:
    """Return the session-stable runtime facts in a fixed order."""

    workspace = cwd.resolve()
    git_marker = workspace / ".git"
    is_git_repository = git_marker.is_dir() or git_marker.is_file()
    shell = os.environ.get("SHELL") or os.environ.get("COMSPEC") or "unknown"
    platform_name = sys.platform or "unknown"
    os_type = platform.system() or "unknown"
    os_version = platform.release() or "unknown"
    return "\n".join(
        (
            f"Workspace: {workspace}",
            f"Git repository: {'yes' if is_git_repository else 'no'}",
            f"Platform: {platform_name}",
            f"Shell: {shell}",
            f"OS: {os_type} {os_version}",
        )
    )


def build_system_prompt_registry(cwd: Path) -> PromptRegistry:
    """Build the system-prompt sections for one workspace session."""

    return PromptRegistry(
        (
            PromptSection(
                "nano-code.identity",
                PromptStability.STATIC,
                lambda: IDENTITY_PROMPT,
            ),
            PromptSection(
                "nano-code.system",
                PromptStability.STATIC,
                lambda: SYSTEM_PROMPT,
            ),
            PromptSection(
                "nano-code.task-guidance",
                PromptStability.STATIC,
                lambda: TASK_GUIDANCE_PROMPT,
            ),
            PromptSection(
                "nano-code.safety",
                PromptStability.STATIC,
                lambda: SAFETY_PROMPT,
            ),
            PromptSection(
                "nano-code.tools",
                PromptStability.STATIC,
                lambda: TOOLS_PROMPT,
            ),
            PromptSection(
                "nano-code.response-style",
                PromptStability.STATIC,
                lambda: RESPONSE_STYLE_PROMPT,
            ),
            PromptSection(
                "nano-code.environment",
                PromptStability.SESSION,
                lambda: _environment_prompt(cwd),
            ),
        )
    )


__all__ = ["build_system_prompt_registry"]
