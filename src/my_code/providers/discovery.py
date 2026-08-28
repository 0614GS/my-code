"""Provider SDK adapters for bounded model discovery and capability resolution."""

import asyncio
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from my_code.config.providers import (
    ANTHROPIC_API_BASE_URL,
    OPENAI_API_BASE_URL,
    ProviderProfile,
    ProviderProtocol,
)
from my_code.model.capabilities import (
    CapabilitySource,
    ModelCapabilities,
    ModelDescriptor,
    ModelLimits,
    fallback_descriptor,
)
from my_code.providers.model_cache import ModelCatalogCache

_MAX_MODELS = 1_000
_OPENAI_CATALOG: dict[str, ModelLimits] = {
    "gpt-4.1": ModelLimits(1_047_576, max_output_tokens=32_768),
    "gpt-4.1-mini": ModelLimits(1_047_576, max_output_tokens=32_768),
    "gpt-4.1-nano": ModelLimits(1_047_576, max_output_tokens=32_768),
    "gpt-4o": ModelLimits(128_000, max_output_tokens=16_384),
    "gpt-4o-mini": ModelLimits(128_000, max_output_tokens=16_384),
    "gpt-5": ModelLimits(400_000, max_output_tokens=128_000),
    "gpt-5.1": ModelLimits(400_000, max_output_tokens=128_000),
    "gpt-5.2": ModelLimits(400_000, max_output_tokens=128_000),
    "gpt-5.4": ModelLimits(400_000, max_output_tokens=128_000),
}


class ProviderProbeError(StrEnum):
    AUTHENTICATION = "authentication"
    ENDPOINT = "endpoint"
    TIMEOUT = "timeout"
    CONNECTION = "connection"
    NO_MODELS = "no-models"
    SERVICE = "service"


@dataclass(frozen=True, slots=True)
class ProviderProbeRequest:
    """Temporary connection details. The key is never persisted by probing."""

    provider_id: str
    protocol: ProviderProtocol
    base_url: str | None
    api_key: str | None = field(default=None, repr=False)
    use_stored_key: bool = False


@dataclass(frozen=True, slots=True)
class ProviderProbeResult:
    """A credential-free snapshot of one online model-catalog attempt."""

    succeeded: bool
    models: tuple[ModelDescriptor, ...]
    probed_at: str
    error_kind: ProviderProbeError | None = None
    error_message: str | None = None
    provider_id: str = ""
    protocol: ProviderProtocol | None = None
    base_url: str | None = None


class AnthropicModelCatalog:
    def __init__(self, client: Any) -> None:
        self.client = client

    async def list_models(self) -> tuple[ModelDescriptor, ...]:
        page = await self.client.models.list(limit=100)
        result: list[ModelDescriptor] = []
        while True:
            for model in page.data:
                result.append(_anthropic_descriptor(model))
                if len(result) >= _MAX_MODELS:
                    return tuple(result)
            if not page.has_next_page():
                return tuple(result)
            page = await page.get_next_page()

    async def resolve_model(self, model_id: str) -> ModelDescriptor | None:
        model = await self.client.models.retrieve(model_id)
        return _anthropic_descriptor(model)


class OpenAIModelCatalog:
    def __init__(self, client: Any, *, official_endpoint: bool) -> None:
        self.client = client
        self.official_endpoint = official_endpoint

    async def list_models(self) -> tuple[ModelDescriptor, ...]:
        page = await self.client.models.list()
        result: list[ModelDescriptor] = []
        while True:
            for model in page.data:
                result.append(self._descriptor(str(model.id)))
                if len(result) >= _MAX_MODELS:
                    return tuple(result)
            if not page.has_next_page():
                return tuple(result)
            page = await page.get_next_page()

    async def resolve_model(self, model_id: str) -> ModelDescriptor | None:
        models = await self.list_models()
        return next((model for model in models if model.id == model_id), None)

    def _descriptor(self, model_id: str) -> ModelDescriptor:
        limits = _OPENAI_CATALOG.get(model_id) if self.official_endpoint else None
        return ModelDescriptor(
            model_id,
            model_id,
            limits or ModelLimits(),
            source=(
                CapabilitySource.BUNDLED_CATALOG
                if limits is not None
                else CapabilitySource.PROVIDER_API
            ),
        )


class ModelDiscoveryService:
    """Refreshes a binding and applies the documented resolution precedence."""

    def __init__(self, cache: ModelCatalogCache) -> None:
        self.cache = cache

    async def probe(
        self,
        request: ProviderProbeRequest,
        *,
        timeout_seconds: float,
    ) -> ProviderProbeResult:
        """Always perform one online catalog request and never touch the cache."""

        probed_at = datetime.now(UTC).isoformat()
        try:
            async with asyncio.timeout(timeout_seconds):
                catalog, close = _catalog_for(
                    request.protocol, request.base_url, request.api_key
                )
                try:
                    models = await catalog.list_models()
                finally:
                    await close()
            if not models:
                return ProviderProbeResult(
                    False,
                    (),
                    probed_at,
                    ProviderProbeError.NO_MODELS,
                    "The endpoint returned an empty model catalog.",
                    provider_id=request.provider_id,
                    protocol=request.protocol,
                    base_url=request.base_url,
                )
            return ProviderProbeResult(
                True,
                models,
                probed_at,
                provider_id=request.provider_id,
                protocol=request.protocol,
                base_url=request.base_url,
            )
        except TimeoutError:
            return ProviderProbeResult(
                False,
                (),
                probed_at,
                ProviderProbeError.TIMEOUT,
                "The model catalog request timed out. Check the Base URL and retry.",
                provider_id=request.provider_id,
                protocol=request.protocol,
                base_url=request.base_url,
            )
        except Exception as error:
            kind, message = _classify_probe_error(error)
            return ProviderProbeResult(
                False,
                (),
                probed_at,
                kind,
                message,
                request.provider_id,
                request.protocol,
                request.base_url,
            )

    async def discover(
        self,
        profile: ProviderProfile,
        *,
        api_key: str | None,
        timeout_seconds: float,
    ) -> tuple[tuple[ModelDescriptor, ...], str | None, str | None]:
        key = self.cache.binding_key(
            profile.id, profile.protocol.value, profile.base_url
        )
        cached = self.cache.load(key)
        if api_key is None and profile.base_url is None:
            error = "No stored API key is configured for startup model discovery."
            if cached is not None:
                return cached.models, cached.fetched_at, error
            return (), None, error
        result = await self.probe(
            ProviderProbeRequest(
                profile.id, profile.protocol, profile.base_url, api_key
            ),
            timeout_seconds=timeout_seconds,
        )
        if result.succeeded:
            models = result.models
            fetched_at = self.cache.save(key, models)
            return models, fetched_at, None
        if cached is not None:
            return cached.models, cached.fetched_at, result.error_message
        return (), None, result.error_message

    async def resolve(
        self,
        profile: ProviderProfile,
        *,
        api_key: str | None,
        timeout_seconds: float,
    ) -> tuple[ModelDescriptor, str | None, str | None]:
        models, fetched_at, error = await self.discover(
            profile, api_key=api_key, timeout_seconds=timeout_seconds
        )
        descriptor = next((item for item in models if item.id == profile.model), None)
        if (
            descriptor is None
            and error is None
            and profile.protocol is ProviderProtocol.ANTHROPIC_MESSAGES
        ):
            try:
                async with asyncio.timeout(timeout_seconds):
                    catalog, close = _catalog(profile, api_key)
                    try:
                        descriptor = await catalog.resolve_model(profile.model)
                    finally:
                        await close()
                if descriptor is not None:
                    models += (descriptor,)
                    fetched_at = self.cache.save(
                        self.cache.binding_key(
                            profile.id, profile.protocol.value, profile.base_url
                        ),
                        models,
                    )
            except Exception as retrieve_error:
                error = str(retrieve_error)
        if descriptor is None and profile.protocol is ProviderProtocol.OPENAI_RESPONSES:
            limits = _OPENAI_CATALOG.get(profile.model)
            if limits is not None and profile.base_url is None:
                descriptor = ModelDescriptor(
                    profile.model,
                    profile.model,
                    limits,
                    source=CapabilitySource.BUNDLED_CATALOG,
                )
        descriptor = descriptor or fallback_descriptor(profile.model)
        if profile.limits.known:
            descriptor = replace(
                descriptor,
                limits=ModelLimits(
                    profile.limits.context_window_tokens
                    or descriptor.limits.context_window_tokens,
                    profile.limits.max_input_tokens
                    or descriptor.limits.max_input_tokens,
                    profile.limits.max_output_tokens
                    or descriptor.limits.max_output_tokens,
                ),
                source=CapabilitySource.PROFILE_OVERRIDE,
            )
        return descriptor, fetched_at, error


def _catalog(profile: ProviderProfile, api_key: str | None) -> tuple[Any, Any]:
    return _catalog_for(profile.protocol, profile.base_url, api_key)


def _catalog_for(
    protocol: ProviderProtocol, base_url: str | None, api_key: str | None
) -> tuple[Any, Any]:
    if protocol is ProviderProtocol.ANTHROPIC_MESSAGES:
        from anthropic import AsyncAnthropic

        anthropic_client = AsyncAnthropic(
            api_key=api_key if api_key is not None else "",
            auth_token="",
            base_url=base_url or ANTHROPIC_API_BASE_URL,
        )
        return AnthropicModelCatalog(anthropic_client), anthropic_client.close
    from openai import AsyncOpenAI

    openai_client = AsyncOpenAI(
        api_key=api_key if api_key is not None else "",
        base_url=base_url or OPENAI_API_BASE_URL,
    )
    official = base_url is None or base_url.rstrip("/") in {
        "https://api.openai.com",
        "https://api.openai.com/v1",
    }
    return (
        OpenAIModelCatalog(openai_client, official_endpoint=official),
        openai_client.close,
    )


def _classify_probe_error(error: Exception) -> tuple[ProviderProbeError, str]:
    status = getattr(error, "status_code", None)
    if status in {401, 403}:
        return (
            ProviderProbeError.AUTHENTICATION,
            "Authentication failed. Check the API key and endpoint permissions.",
        )
    if status == 404:
        return (
            ProviderProbeError.ENDPOINT,
            "The model catalog endpoint was not found. "
            "Check the Base URL and protocol.",
        )
    name = type(error).__name__.casefold()
    if "timeout" in name:
        return (
            ProviderProbeError.TIMEOUT,
            "The model catalog request timed out. Check the Base URL and retry.",
        )
    if any(token in name for token in ("connection", "connect", "network")):
        return (
            ProviderProbeError.CONNECTION,
            "Could not connect to the endpoint. Check the Base URL and network.",
        )
    if isinstance(status, int) and 400 <= status < 500:
        return (
            ProviderProbeError.ENDPOINT,
            "The endpoint rejected the model catalog request. "
            "Check the protocol and Base URL.",
        )
    return (
        ProviderProbeError.SERVICE,
        "The provider could not return its model catalog. "
        "Retry or configure a model manually.",
    )


def _anthropic_descriptor(model: object) -> ModelDescriptor:
    raw = model.model_dump(mode="python")  # type: ignore[attr-defined]
    capabilities = raw.get("capabilities") or {}
    return ModelDescriptor(
        str(raw["id"]),
        str(raw.get("display_name") or raw["id"]),
        ModelLimits(
            max_input_tokens=_positive_or_none(raw.get("max_input_tokens")),
            max_output_tokens=_positive_or_none(raw.get("max_tokens")),
        ),
        ModelCapabilities(
            _supported(capabilities.get("thinking")),
            _supported(capabilities.get("effort")),
            _supported(capabilities.get("context_management")),
        ),
        CapabilitySource.PROVIDER_API,
    )


def _positive_or_none(value: object) -> int | None:
    return (
        value
        if isinstance(value, int) and not isinstance(value, bool) and value > 0
        else None
    )


def _supported(value: object) -> bool | None:
    if value is None:
        return None
    if isinstance(value, dict):
        supported = value.get("supported")
        return supported if isinstance(supported, bool) else None
    supported = getattr(value, "supported", None)
    return supported if isinstance(supported, bool) else None


__all__ = [
    "AnthropicModelCatalog",
    "ModelDiscoveryService",
    "OpenAIModelCatalog",
    "ProviderProbeError",
    "ProviderProbeRequest",
    "ProviderProbeResult",
    "resolve_without_network",
]


def resolve_without_network(
    protocol: ProviderProtocol,
    base_url: str | None,
    model_id: str,
    limits_override: ModelLimits,
) -> ModelDescriptor:
    if limits_override.known:
        return ModelDescriptor(
            model_id,
            model_id,
            limits_override,
            source=CapabilitySource.PROFILE_OVERRIDE,
        )
    if protocol is ProviderProtocol.OPENAI_RESPONSES and base_url is None:
        bundled = _OPENAI_CATALOG.get(model_id)
        if bundled is not None:
            return ModelDescriptor(
                model_id,
                model_id,
                bundled,
                source=CapabilitySource.BUNDLED_CATALOG,
            )
    return fallback_descriptor(model_id)
