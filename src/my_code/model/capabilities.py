"""Provider-neutral model capabilities, limits, and active environment."""

from dataclasses import dataclass
from enum import StrEnum


class CapabilitySource(StrEnum):
    PROVIDER_API = "provider_api"
    CACHE = "cache"
    BUNDLED_CATALOG = "bundled_catalog"
    PROFILE_OVERRIDE = "profile_override"
    FALLBACK = "fallback"


class ModelCompatibility(StrEnum):
    """Whether a catalog entry is known to support the profile protocol."""

    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ModelLimits:
    context_window_tokens: int | None = None
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None

    def __post_init__(self) -> None:
        for name, value in (
            ("context_window_tokens", self.context_window_tokens),
            ("max_input_tokens", self.max_input_tokens),
            ("max_output_tokens", self.max_output_tokens),
        ):
            if value is not None and value < 1:
                raise ValueError(f"{name} must be positive or unknown")

    @property
    def known(self) -> bool:
        return any(
            value is not None
            for value in (
                self.context_window_tokens,
                self.max_input_tokens,
                self.max_output_tokens,
            )
        )

    def effective_input_limit(self, requested_output_tokens: int) -> int | None:
        candidates: list[int] = []
        if self.max_input_tokens is not None:
            candidates.append(self.max_input_tokens)
        if self.context_window_tokens is not None:
            candidates.append(
                max(1, self.context_window_tokens - requested_output_tokens)
            )
        return min(candidates) if candidates else None


@dataclass(frozen=True, slots=True)
class ModelCapabilities:
    """Provider-neutral, allowlisted model metadata.

    ``None`` means that the catalog did not say.  In particular, it must not be
    interpreted as ``False``.
    """

    thinking: bool | None = None
    effort: bool | None = None
    context_management: bool | None = None
    input_modalities: tuple[str, ...] | None = None
    output_modalities: tuple[str, ...] | None = None
    streaming: bool | None = None
    tool_calling: bool | None = None
    structured_outputs: bool | None = None
    citations: bool | None = None
    batch: bool | None = None
    code_execution: bool | None = None
    request_parameters: tuple[str, ...] | None = None
    reasoning_efforts: tuple[str, ...] | None = None
    reasoning_adaptive: bool | None = None
    reasoning_budgeted: bool | None = None
    reasoning_context_modes: tuple[str, ...] | None = None
    reasoning_pro_mode: bool | None = None


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
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


@dataclass(frozen=True, slots=True)
class ModelDescriptor:
    id: str
    display_name: str | None = None
    limits: ModelLimits = ModelLimits()
    capabilities: ModelCapabilities = ModelCapabilities()
    source: CapabilitySource = CapabilitySource.PROVIDER_API
    compatibility: ModelCompatibility = ModelCompatibility.UNKNOWN
    created_at: str | None = None
    owned_by: str | None = None
    shutdown_at: str | None = None
    fallback_model_ids: tuple[str, ...] | None = None
    metadata_sources: tuple[str, ...] = ()
    discovered_at: str | None = None
    user_defined: bool = False

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ValueError("Model id must not be empty")
        if self.display_name is not None and not self.display_name.strip():
            raise ValueError("Model display name must not be empty")

    @property
    def label(self) -> str:
        return self.display_name or self.id

    @property
    def selectable(self) -> bool:
        return self.compatibility is not ModelCompatibility.UNSUPPORTED


@dataclass(frozen=True, slots=True)
class ActiveModelEnvironment:
    descriptor: ModelDescriptor
    compact_trigger_tokens: int
    configured_compact_trigger_tokens: int | None = None
    discovered_at: str | None = None
    discovery_error: str | None = None
    warning: str | None = None


FALLBACK_INPUT_TOKENS = 200_000


def resolve_environment(
    descriptor: ModelDescriptor,
    *,
    requested_output_tokens: int,
    configured_trigger_tokens: int | None,
    discovered_at: str | None = None,
    discovery_error: str | None = None,
) -> ActiveModelEnvironment:
    effective_output_tokens = min(
        requested_output_tokens,
        descriptor.limits.max_output_tokens or requested_output_tokens,
    )
    limit = descriptor.limits.effective_input_limit(effective_output_tokens)
    if limit is None:
        limit = FALLBACK_INPUT_TOKENS
    automatic = max(1, limit * 9 // 10)
    warning = None
    trigger = (
        automatic if configured_trigger_tokens is None else configured_trigger_tokens
    )
    if trigger > limit:
        warning = (
            f"Configured compact threshold {trigger} exceeds the resolved input "
            f"limit {limit}; using {limit}."
        )
        trigger = limit
    return ActiveModelEnvironment(
        descriptor=descriptor,
        compact_trigger_tokens=trigger,
        configured_compact_trigger_tokens=configured_trigger_tokens,
        discovered_at=discovered_at,
        discovery_error=discovery_error,
        warning=warning,
    )


def fallback_descriptor(model_id: str) -> ModelDescriptor:
    return ModelDescriptor(
        model_id,
        model_id,
        ModelLimits(max_input_tokens=FALLBACK_INPUT_TOKENS),
        source=CapabilitySource.FALLBACK,
    )


__all__ = [
    "ActiveModelEnvironment",
    "CapabilitySource",
    "FALLBACK_INPUT_TOKENS",
    "ModelCapabilities",
    "ModelCompatibility",
    "ModelDescriptor",
    "ModelLimits",
    "ProviderCapabilities",
    "fallback_descriptor",
    "resolve_environment",
]
