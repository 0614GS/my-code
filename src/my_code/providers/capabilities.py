"""Provider-neutral capability rules that are safe on the startup import path."""

from my_code.config.providers import ProviderProtocol
from my_code.model.capabilities import ProviderCapabilities


def capabilities_for(
    protocol: ProviderProtocol, base_url: str | None
) -> ProviderCapabilities:
    if protocol is ProviderProtocol.ANTHROPIC_MESSAGES and base_url is None:
        return ProviderCapabilities(
            system_prompt_blocks=True,
            prompt_caching=True,
            max_prompt_cache_breakpoints=2,
        )
    return ProviderCapabilities()


__all__ = ["capabilities_for"]
