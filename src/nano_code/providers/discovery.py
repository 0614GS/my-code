"""Provider SDK adapters for bounded model discovery and capability resolution."""

import asyncio
from dataclasses import replace
from typing import Any

from anthropic import AsyncAnthropic
from openai import AsyncOpenAI

from nano_code.config.providers import ProviderProfile, ProviderProtocol
from nano_code.model.capabilities import (
    CapabilitySource,
    ModelCapabilities,
    ModelDescriptor,
    ModelLimits,
    fallback_descriptor,
)
from nano_code.providers.model_cache import ModelCatalogCache

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


class AnthropicModelCatalog:
    def __init__(self, client: AsyncAnthropic) -> None:
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
    def __init__(self, client: AsyncOpenAI, *, official_endpoint: bool) -> None:
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
        try:
            async with asyncio.timeout(timeout_seconds):
                catalog, close = _catalog(profile, api_key)
                try:
                    models = await catalog.list_models()
                finally:
                    await close()
            fetched_at = self.cache.save(key, models)
            return models, fetched_at, None
        except Exception as error:
            if cached is not None:
                return cached.models, cached.fetched_at, str(error)
            return (), None, str(error)

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
    if profile.protocol is ProviderProtocol.ANTHROPIC_MESSAGES:
        anthropic_client = AsyncAnthropic(api_key=api_key, base_url=profile.base_url)
        return AnthropicModelCatalog(anthropic_client), anthropic_client.close
    openai_client = AsyncOpenAI(api_key=api_key, base_url=profile.base_url)
    official = profile.base_url is None or profile.base_url.rstrip("/") in {
        "https://api.openai.com",
        "https://api.openai.com/v1",
    }
    return (
        OpenAIModelCatalog(openai_client, official_endpoint=official),
        openai_client.close,
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
