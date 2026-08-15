"""provider adapter 的能力值。"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    """由 provider 声明、组合根只读的可选协议能力。"""

    system_prompt_blocks: bool = False
    prompt_caching: bool = False
    max_prompt_cache_breakpoints: int = 0

    def __post_init__(self) -> None:
        if self.max_prompt_cache_breakpoints < 0:
            raise ValueError("Cache breakpoint count must not be negative")
        if self.prompt_caching and not self.system_prompt_blocks:
            raise ValueError("Prompt caching requires structured system blocks")
        if self.prompt_caching and self.max_prompt_cache_breakpoints < 1:
            raise ValueError("Prompt caching requires at least one breakpoint")


__all__ = ["ProviderCapabilities"]
