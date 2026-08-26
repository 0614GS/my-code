"""Fixed built-in Subagent role definitions."""

from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType

from my_code.features.subagents.models import (
    SubagentDefinition,
    SubagentType,
)
from my_code.features.subagents.prompts import (
    build_explore_prompt_registry,
    build_general_prompt_registry,
)

EXPLORE_TOOL_NAMES = ("Read", "Glob", "Grep", "Bash")


def build_subagent_definitions(
    cwd: Path,
) -> Mapping[SubagentType, SubagentDefinition]:
    definitions = {
        SubagentType.EXPLORE: SubagentDefinition(
            SubagentType.EXPLORE,
            "Read-only repository research with file-based evidence",
            build_explore_prompt_registry(cwd),
            EXPLORE_TOOL_NAMES,
            read_only=True,
        ),
        SubagentType.GENERAL: SubagentDefinition(
            SubagentType.GENERAL,
            "General coding, implementation, and verification work",
            build_general_prompt_registry(cwd),
            None,
        ),
    }
    return MappingProxyType(definitions)


__all__ = ["EXPLORE_TOOL_NAMES", "build_subagent_definitions"]
