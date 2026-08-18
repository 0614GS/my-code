"""Provider-neutral model discovery and context-limit contracts."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class CapabilitySource(StrEnum):
    PROVIDER_API = "provider_api"
    CACHE = "cache"
    BUNDLED_CATALOG = "bundled_catalog"
    PROFILE_OVERRIDE = "profile_override"
    FALLBACK = "fallback"


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
    thinking: bool | None = None
    effort: bool | None = None
    context_management: bool | None = None


@dataclass(frozen=True, slots=True)
class ModelDescriptor:
    id: str
    display_name: str
    limits: ModelLimits = ModelLimits()
    capabilities: ModelCapabilities = ModelCapabilities()
    source: CapabilitySource = CapabilitySource.PROVIDER_API

    def __post_init__(self) -> None:
        if not self.id.strip() or not self.display_name.strip():
            raise ValueError("Model id and display name must not be empty")


class ProviderModelCatalogPort(Protocol):
    async def list_models(self) -> tuple[ModelDescriptor, ...]: ...

    async def resolve_model(self, model_id: str) -> ModelDescriptor | None: ...


@dataclass(frozen=True, slots=True)
class ActiveModelEnvironment:
    descriptor: ModelDescriptor
    compact_trigger_tokens: int
    configured_compact_trigger_tokens: int | None = None
    discovered_at: str | None = None
    discovery_error: str | None = None
    warning: str | None = None


@dataclass(slots=True)
class ActiveModelState:
    """Shared mutable state switched atomically between model calls."""

    environment: ActiveModelEnvironment

    def get(self) -> ActiveModelEnvironment:
        return self.environment

    def set(self, environment: ActiveModelEnvironment) -> None:
        self.environment = environment


FALLBACK_INPUT_TOKENS = 200_000


def resolve_environment(
    descriptor: ModelDescriptor,
    *,
    requested_output_tokens: int,
    configured_trigger_tokens: int | None,
    discovered_at: str | None = None,
    discovery_error: str | None = None,
) -> ActiveModelEnvironment:
    limit = descriptor.limits.effective_input_limit(requested_output_tokens)
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
    "ActiveModelState",
    "CapabilitySource",
    "FALLBACK_INPUT_TOKENS",
    "ModelCapabilities",
    "ModelDescriptor",
    "ModelLimits",
    "ProviderModelCatalogPort",
    "fallback_descriptor",
    "resolve_environment",
]
