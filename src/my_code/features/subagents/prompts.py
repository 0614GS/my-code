"""Dedicated prompt profiles for the built-in child agent roles."""

from pathlib import Path

from my_code.model.request import PromptStability
from my_code.prompts.defaults import RESPONSE_STYLE_PROMPT, SAFETY_PROMPT
from my_code.prompts.models import PromptSection
from my_code.prompts.registry import PromptRegistry
from my_code.prompts.system import environment_prompt

_EXPLORE_IDENTITY = """You are Explore, a read-only repository researcher
working for a parent agent. Investigate only the explicit task you were given.
Base conclusions on workspace evidence,
cite file paths and line numbers when useful, and clearly mark uncertainty.
Do not implement, edit, create, delete, or otherwise modify anything."""

_EXPLORE_TOOLS = """Use only the available read-only inspection tools.
Read, Glob, and Grep inspect files. Bash may only run commands whose semantics
are read-only. Bash already starts in the workspace, so do not prefix commands
with a redundant cd to that directory. Never work around the boundary or
delegate to another agent."""

_GENERAL_IDENTITY = """You are General, an isolated coding agent working for
a parent agent.
Complete only the explicit task you were given. You may inspect, modify, and verify the
workspace with the available tools, and may delegate when that materially helps."""

_GENERAL_TASK = """Read relevant files before changing them and preserve unrelated work.
Perform reasonable verification before finishing. Report to the parent agent
what changed, what you verified, and any unresolved risks. Do not address an
end user or assume access to the parent conversation; all task context must
come from the explicit prompt and attachments."""


def build_explore_prompt_registry(cwd: Path) -> PromptRegistry:
    return _build_registry(cwd, _EXPLORE_IDENTITY, _EXPLORE_TOOLS)


def build_general_prompt_registry(cwd: Path) -> PromptRegistry:
    return _build_registry(cwd, _GENERAL_IDENTITY, _GENERAL_TASK)


def _build_registry(cwd: Path, identity: str, tools: str) -> PromptRegistry:
    return PromptRegistry(
        (
            PromptSection(
                "subagent.identity", PromptStability.STATIC, lambda: identity
            ),
            PromptSection(
                "subagent.safety", PromptStability.STATIC, lambda: SAFETY_PROMPT
            ),
            PromptSection("subagent.tools", PromptStability.STATIC, lambda: tools),
            PromptSection(
                "subagent.response-style",
                PromptStability.STATIC,
                lambda: RESPONSE_STYLE_PROMPT,
            ),
            PromptSection(
                "subagent.environment",
                PromptStability.SESSION,
                lambda: environment_prompt(cwd),
            ),
        )
    )


__all__ = ["build_explore_prompt_registry", "build_general_prompt_registry"]
