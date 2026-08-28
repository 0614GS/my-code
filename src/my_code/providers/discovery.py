"""Provider SDK adapters for bounded model discovery and capability resolution."""

import asyncio
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, cast

from my_code.config.providers import (
    ANTHROPIC_API_BASE_URL,
    OPENAI_API_BASE_URL,
    ProviderProfile,
    ProviderProtocol,
)
from my_code.model.capabilities import (
    CapabilitySource,
    ModelCapabilities,
    ModelCompatibility,
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
_OPENAI_UNSUPPORTED = {
    "dall-e-2",
    "dall-e-3",
    "text-embedding-3-large",
    "text-embedding-3-small",
    "text-embedding-ada-002",
    "tts-1",
    "tts-1-hd",
    "whisper-1",
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
                result.append(self._descriptor(model))
                if len(result) >= _MAX_MODELS:
                    return tuple(result)
            if not page.has_next_page():
                return tuple(result)
            page = await page.get_next_page()

    async def resolve_model(self, model_id: str) -> ModelDescriptor | None:
        models = await self.list_models()
        return next((model for model in models if model.id == model_id), None)

    def _descriptor(self, model: object) -> ModelDescriptor:
        typed = cast(Any, model)
        model_id = str(typed.id)
        limits = _OPENAI_CATALOG.get(model_id) if self.official_endpoint else None
        created = getattr(model, "created", None)
        created_at = (
            datetime.fromtimestamp(created, UTC).isoformat()
            if isinstance(created, int)
            and not isinstance(created, bool)
            and created >= 0
            else None
        )
        owned_by = getattr(model, "owned_by", None)
        official = limits is not None
        known_unsupported = self.official_endpoint and model_id in _OPENAI_UNSUPPORTED
        return ModelDescriptor(
            model_id,
            model_id,
            limits or ModelLimits(),
            ModelCapabilities(
                thinking=True if official and model_id.startswith("gpt-5") else None,
                effort=True if official and model_id.startswith("gpt-5") else None,
                input_modalities=("text", "image") if official else None,
                output_modalities=("text",) if official else None,
                streaming=True if official else None,
                tool_calling=True if official else None,
                structured_outputs=True if official else None,
            ),
            source=(
                CapabilitySource.BUNDLED_CATALOG
                if limits is not None
                else CapabilitySource.PROVIDER_API
            ),
            compatibility=(
                ModelCompatibility.SUPPORTED
                if official
                else (
                    ModelCompatibility.UNSUPPORTED
                    if known_unsupported
                    else ModelCompatibility.UNKNOWN
                )
            ),
            created_at=created_at,
            owned_by=owned_by if isinstance(owned_by, str) and owned_by else None,
            metadata_sources=(
                (
                    CapabilitySource.PROVIDER_API.value,
                    CapabilitySource.BUNDLED_CATALOG.value,
                )
                if official
                else (CapabilitySource.PROVIDER_API.value,)
            ),
        )


class ModelDiscoveryService:
    """Compatibility facade for online probing without cache writes."""

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
            discovered = tuple(
                replace(model, discovered_at=probed_at) for model in models
            )
            return ProviderProbeResult(
                True,
                discovered,
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
        result = await self.probe(
            ProviderProbeRequest(
                profile.id, profile.protocol, profile.base_url, api_key
            ),
            timeout_seconds=timeout_seconds,
        )
        if result.succeeded:
            return result.models, result.probed_at, None
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
        if descriptor is None:
            descriptor = next(
                (item for item in profile.models if item.id == profile.model), None
            )
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
    thinking = capabilities.get("thinking") or {}
    effort = capabilities.get("effort") or {}
    context_management = capabilities.get("context_management") or {}
    created_at = raw.get("created_at")
    fallback_ids = raw.get("fallback_model_ids")
    efforts = _strings_or_none(
        effort.get("levels") if isinstance(effort, dict) else None
    ) or _strings_or_none(capabilities.get("reasoning_efforts"))
    if efforts is None and _supported(effort) is False:
        efforts = ()
    return ModelDescriptor(
        str(raw["id"]),
        str(raw.get("display_name") or raw["id"]),
        ModelLimits(
            max_input_tokens=_positive_or_none(raw.get("max_input_tokens")),
            max_output_tokens=_positive_or_none(raw.get("max_tokens")),
        ),
        ModelCapabilities(
            _supported(thinking),
            _supported(effort),
            _supported(context_management),
            input_modalities=_strings_or_none(capabilities.get("input_modalities")),
            output_modalities=_strings_or_none(capabilities.get("output_modalities")),
            streaming=_supported(capabilities.get("streaming")),
            tool_calling=_supported(capabilities.get("tool_use")),
            structured_outputs=_supported(capabilities.get("structured_outputs")),
            citations=_supported(capabilities.get("citations")),
            batch=_supported(capabilities.get("batch")),
            code_execution=_supported(capabilities.get("code_execution")),
            request_parameters=_strings_or_none(capabilities.get("request_parameters")),
            reasoning_efforts=efforts,
            reasoning_adaptive=_dict_bool(thinking, "adaptive"),
            reasoning_budgeted=_dict_bool(thinking, "budgeted"),
            reasoning_context_modes=_strings_or_none(
                capabilities.get("reasoning_context_modes")
            ),
            reasoning_pro_mode=_dict_bool(thinking, "pro_mode"),
        ),
        CapabilitySource.PROVIDER_API,
        compatibility=ModelCompatibility.SUPPORTED,
        created_at=_iso_or_none(created_at),
        shutdown_at=_iso_or_none(raw.get("shutdown_at")),
        fallback_model_ids=_strings_or_none(fallback_ids),
        metadata_sources=(CapabilitySource.PROVIDER_API.value,),
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


def _dict_bool(value: object, key: str) -> bool | None:
    if not isinstance(value, dict):
        return None
    result = value.get(key)
    return result if isinstance(result, bool) else None


def _strings_or_none(value: object) -> tuple[str, ...] | None:
    if not isinstance(value, (list, tuple)):
        return None
    result = tuple(item for item in value if isinstance(item, str) and item)
    return tuple(dict.fromkeys(result)) or None


def _iso_or_none(value: object) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    return value if isinstance(value, str) and value else None


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
