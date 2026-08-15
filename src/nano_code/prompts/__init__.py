"""提示词来源、生命周期与模型请求投影。"""

from nano_code.prompts.defaults import default_prompt_registry
from nano_code.prompts.models import (
    PromptSection,
    PromptStability,
    ResolvedPromptSection,
    SystemPrompt,
)
from nano_code.prompts.registry import PromptRegistry

__all__ = [
    "PromptRegistry",
    "PromptSection",
    "PromptStability",
    "ResolvedPromptSection",
    "SystemPrompt",
    "default_prompt_registry",
]
