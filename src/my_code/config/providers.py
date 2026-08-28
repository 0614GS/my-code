"""按协议配置的具名 provider profile。"""

import json
import os
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from my_code.config.validation import validate_base_url
from my_code.model.capabilities import (
    CapabilitySource,
    ModelCapabilities,
    ModelCompatibility,
    ModelDescriptor,
    ModelLimits,
)
from my_code.model.primitives import validate_provider_id

_SCHEMA_VERSION = 4
_MAX_MODELS = 1_000
ANTHROPIC_API_BASE_URL = "https://api.anthropic.com"
OPENAI_API_BASE_URL = "https://api.openai.com/v1"


class ProviderProfileError(ValueError):
    """provider profile 目录格式错误或无法持久化。"""


class ProviderProtocol(StrEnum):
    """my-code 支持的线路协议。"""

    ANTHROPIC_MESSAGES = "anthropic-messages"
    OPENAI_RESPONSES = "openai-responses"


@dataclass(frozen=True, slots=True)
class ReasoningConfig:
    enabled: bool = True
    effort: str = "auto"
    context: str = "auto"

    def for_protocol(self, protocol: ProviderProtocol) -> "ReasoningConfig":
        anthropic = {"auto", "low", "medium", "high", "max"}
        openai = {"auto", "none", "minimal", "low", "medium", "high", "xhigh", "max"}
        allowed = (
            anthropic if protocol is ProviderProtocol.ANTHROPIC_MESSAGES else openai
        )
        if self.effort not in allowed:
            raise ProviderProfileError(
                f"Unsupported {protocol.value} reasoning effort: {self.effort}"
            )
        if self.context not in {"auto", "current_turn", "all_turns"}:
            raise ProviderProfileError(f"Unsupported reasoning context: {self.context}")
        if protocol is ProviderProtocol.ANTHROPIC_MESSAGES and self.context != "auto":
            raise ProviderProfileError("Anthropic reasoning context must be auto")
        return self


@dataclass(frozen=True, slots=True)
class CompactConfig:
    trigger_input_tokens: int | None = None

    def __post_init__(self) -> None:
        if self.trigger_input_tokens is not None and self.trigger_input_tokens < 1:
            raise ProviderProfileError("compact triggerInputTokens must be positive")


@dataclass(frozen=True, slots=True)
class ProviderProfile:
    """构造模型 provider 适配器所需的非敏感设置。"""

    id: str
    protocol: ProviderProtocol
    model: str
    base_url: str | None = None
    reasoning: ReasoningConfig = ReasoningConfig()
    limits: ModelLimits = ModelLimits()
    compact: CompactConfig = CompactConfig()
    models: tuple[ModelDescriptor, ...] = ()

    def __post_init__(self) -> None:
        try:
            validate_provider_id(self.id)
        except ValueError as error:
            raise ProviderProfileError(str(error)) from error
        if not self.model.strip():
            raise ProviderProfileError("provider model must be a non-empty string")
        models = _deduplicate_models(self.models)
        if not models:
            models = (
                ModelDescriptor(
                    self.model,
                    self.model,
                    self.limits,
                    source=(
                        CapabilitySource.PROFILE_OVERRIDE
                        if self.limits.known
                        else CapabilitySource.FALLBACK
                    ),
                    user_defined=True,
                ),
            )
        selected = next((item for item in models if item.id == self.model), None)
        if selected is None:
            raise ProviderProfileError(
                "provider defaultModel must reference a model in models"
            )
        if not selected.selectable:
            raise ProviderProfileError("provider defaultModel must be selectable")
        if self.limits.known and selected.limits != self.limits:
            selected = _with_limits(selected, self.limits)
            models = tuple(
                selected if item.id == selected.id else item for item in models
            )
        object.__setattr__(self, "models", models)
        object.__setattr__(self, "limits", selected.limits)
        if self.base_url is not None:
            try:
                normalized = validate_base_url(self.base_url)
            except ValueError as error:
                raise ProviderProfileError(
                    f"invalid provider base URL: {error}"
                ) from error
            object.__setattr__(self, "base_url", normalized)
        self.reasoning.for_protocol(self.protocol)
        known_limit = self.limits.effective_input_limit(
            self.limits.max_output_tokens or 1
        )
        if (
            known_limit is not None
            and self.compact.trigger_input_tokens is not None
            and self.compact.trigger_input_tokens > known_limit
        ):
            raise ProviderProfileError(
                "compact triggerInputTokens exceeds the profile model input limit"
            )

    @property
    def selected_model(self) -> ModelDescriptor:
        return next(item for item in self.models if item.id == self.model)


class ProviderProfileStore:
    """原子持久化用户所有且不含凭据的 provider profile。"""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> dict[str, ProviderProfile]:
        if not self.path.exists():
            return {}
        try:
            raw: object = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ProviderProfileError(
                f"Cannot read provider profiles {self.path}: {error}"
            ) from error
        if not isinstance(raw, dict) or raw.get("version") not in {2, 3, 4}:
            raise ProviderProfileError(
                "Provider profiles must use schema version "
                f"{_SCHEMA_VERSION}: {self.path}. Recreate the provider profile."
            )
        providers = raw.get("providers")
        if not isinstance(providers, dict):
            raise ProviderProfileError(f"providers must be an object: {self.path}")

        result: dict[str, ProviderProfile] = {}
        for provider_id, value in providers.items():
            if not isinstance(provider_id, str) or not isinstance(value, dict):
                raise ProviderProfileError(
                    f"Each provider must be a named object: {self.path}"
                )
            result[provider_id] = _parse_profile(
                provider_id,
                value,
                self.path,
                legacy=raw.get("version") == 2,
                token_schema=raw.get("version") in {3, 4},
                catalog_schema=raw.get("version") == 4,
            )
        if raw.get("version") in {2, 3}:
            result = self._merge_legacy_cache(result)
        return result

    def _merge_legacy_cache(
        self, profiles: dict[str, ProviderProfile]
    ) -> dict[str, ProviderProfile]:
        cache = _legacy_cache(self.path.parent / ".model-catalog.json")
        if not cache:
            return profiles
        merged: dict[str, ProviderProfile] = {}
        for provider_id, profile in profiles.items():
            endpoint = (profile.base_url or "<sdk-default>").rstrip("/").lower()
            key = f"{provider_id}|{profile.protocol.value}|{endpoint}"
            cached = cache.get(key, ())
            models = _merge_catalog(cached, profile.models)
            merged[provider_id] = _replace_profile_models(profile, models)
        return merged

    def ensure_empty_exists(self) -> bool:
        """Create an empty catalog without inventing a provider profile."""

        if self.path.exists():
            self.load()
            return False
        self.write(())
        return True

    def write(self, profiles: Iterable[ProviderProfile]) -> None:
        indexed = {profile.id: profile for profile in profiles}
        existing: dict[str, object] = {}
        existing_providers: dict[object, object] = {}
        if self.path.exists():
            self.load()
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                existing = dict(loaded)
                raw_providers = loaded.get("providers")
                if isinstance(raw_providers, dict):
                    existing_providers = raw_providers
        providers: dict[str, object] = {}
        for provider_id in sorted(indexed):
            previous = existing_providers.get(provider_id)
            entry = dict(previous) if isinstance(previous, dict) else {}
            entry.pop("limits", None)
            entry.update(_profile_document(indexed[provider_id]))
            providers[provider_id] = entry
        document = dict(existing)
        document.update(version=_SCHEMA_VERSION, providers=providers)
        atomic_private_json_write(self.path, document)


def _parse_profile(
    provider_id: str,
    raw: dict[object, object],
    path: Path,
    *,
    legacy: bool = False,
    token_schema: bool = False,
    catalog_schema: bool = False,
) -> ProviderProfile:
    model = raw.get("defaultModel")
    protocol = raw.get("protocol")
    base_url = raw.get("baseUrl")
    reasoning_raw = raw.get("reasoning")
    if not isinstance(model, str):
        raise ProviderProfileError(f"provider model must be a string: {path}")
    if not isinstance(protocol, str):
        raise ProviderProfileError(f"provider protocol must be a string: {path}")
    if base_url is not None and not isinstance(base_url, str):
        raise ProviderProfileError(f"provider baseUrl must be a string: {path}")
    try:
        parsed_protocol = ProviderProtocol(protocol)
    except ValueError as error:
        raise ProviderProfileError(
            f"Unsupported provider protocol {protocol!r}: {path}"
        ) from error
    if legacy:
        reasoning = ReasoningConfig(enabled=False)
    else:
        if reasoning_raw is None:
            reasoning = ReasoningConfig()
        else:
            if not isinstance(reasoning_raw, dict):
                raise ProviderProfileError(
                    f"provider reasoning must be an object: {path}"
                )
            enabled = reasoning_raw.get("enabled", True)
            effort = reasoning_raw.get("effort", "auto")
            context = reasoning_raw.get("context", "auto")
            if (
                not isinstance(enabled, bool)
                or not isinstance(effort, str)
                or not isinstance(context, str)
            ):
                raise ProviderProfileError(f"invalid provider reasoning config: {path}")
            reasoning = ReasoningConfig(enabled, effort, context)
    limits = ModelLimits()
    compact = CompactConfig()
    if token_schema:
        limits = _parse_limits(raw.get("limits"), path)
        compact = _parse_compact(raw.get("compact"), path)
    models = _parse_models(raw.get("models"), path) if catalog_schema else ()
    if catalog_schema and not models:
        raise ProviderProfileError(f"provider models must not be empty: {path}")
    return ProviderProfile(
        id=provider_id,
        model=model,
        protocol=parsed_protocol,
        base_url=base_url,
        reasoning=reasoning,
        limits=limits,
        compact=compact,
        models=models,
    )


def _profile_document(profile: ProviderProfile) -> dict[str, object]:
    document: dict[str, object] = {
        "protocol": profile.protocol.value,
        "defaultModel": profile.model,
        "reasoning": {
            "enabled": profile.reasoning.enabled,
            "effort": profile.reasoning.effort,
            "context": profile.reasoning.context,
        },
        "models": [_model_document(model) for model in profile.models],
        "compact": {
            "triggerInputTokens": profile.compact.trigger_input_tokens,
        },
    }
    if profile.base_url is not None:
        document["baseUrl"] = profile.base_url
    return document


def _parse_limits(value: object, path: Path) -> ModelLimits:
    if value is None:
        return ModelLimits()
    if not isinstance(value, dict):
        raise ProviderProfileError(f"provider limits must be an object: {path}")
    return ModelLimits(
        _optional_positive(value, "contextWindowTokens", path),
        _optional_positive(value, "maxInputTokens", path),
        _optional_positive(value, "maxOutputTokens", path),
    )


def _parse_compact(value: object, path: Path) -> CompactConfig:
    if value is None:
        return CompactConfig()
    if not isinstance(value, dict):
        raise ProviderProfileError(f"provider compact must be an object: {path}")
    return CompactConfig(_optional_positive(value, "triggerInputTokens", path))


def _optional_positive(raw: dict[object, object], key: str, path: Path) -> int | None:
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ProviderProfileError(f"provider {key} must be positive or null: {path}")
    return value


def _parse_models(value: object, path: Path) -> tuple[ModelDescriptor, ...]:
    if not isinstance(value, list) or len(value) > _MAX_MODELS:
        raise ProviderProfileError(
            f"provider models must be an array of at most {_MAX_MODELS} items: {path}"
        )
    try:
        models = tuple(_parse_model(item, path) for item in value)
        if len({model.id for model in models}) != len(models):
            raise ValueError("model IDs must be unique")
        return models
    except ValueError as error:
        raise ProviderProfileError(
            f"invalid provider model catalog: {path}: {error}"
        ) from error


def _parse_model(value: object, path: Path) -> ModelDescriptor:
    if not isinstance(value, dict):
        raise ProviderProfileError(f"provider model must be an object: {path}")
    model_id = value.get("id")
    if not isinstance(model_id, str):
        raise ProviderProfileError(f"provider model id must be a string: {path}")
    display_name = _optional_string(value.get("displayName"), "displayName")
    compatibility_raw = value.get("compatibility", ModelCompatibility.UNKNOWN.value)
    if not isinstance(compatibility_raw, str):
        raise ProviderProfileError(
            f"provider model compatibility must be a string: {path}"
        )
    capabilities = value.get("capabilities")
    if capabilities is not None and not isinstance(capabilities, dict):
        raise ProviderProfileError(
            f"provider model capabilities must be an object: {path}"
        )
    capability_values = capabilities or {}
    reasoning = capability_values.get("reasoning")
    if reasoning is not None and not isinstance(reasoning, dict):
        raise ProviderProfileError(
            f"provider model reasoning must be an object: {path}"
        )
    reasoning_values = reasoning or {}
    sources = _optional_strings(value.get("metadataSources"), "metadataSources") or ()
    fallbacks = _optional_strings(value.get("fallbackModelIds"), "fallbackModelIds")
    limits = _parse_limits(value.get("limits"), path)
    user_defined = _required_optional_bool(
        value.get("userDefined", False), "userDefined"
    )
    reasoning_efforts = _optional_strings(
        reasoning_values.get("efforts"), "reasoning.efforts"
    )
    return ModelDescriptor(
        id=model_id,
        display_name=display_name,
        limits=limits,
        capabilities=ModelCapabilities(
            thinking=_optional_bool(reasoning_values.get("supported")),
            effort=None if reasoning_efforts is None else bool(reasoning_efforts),
            context_management=_optional_bool(
                capability_values.get("contextManagement")
            ),
            input_modalities=_optional_strings(
                capability_values.get("inputModalities"), "inputModalities"
            ),
            output_modalities=_optional_strings(
                capability_values.get("outputModalities"), "outputModalities"
            ),
            streaming=_optional_bool(capability_values.get("streaming")),
            tool_calling=_optional_bool(capability_values.get("toolCalling")),
            structured_outputs=_optional_bool(
                capability_values.get("structuredOutputs")
            ),
            citations=_optional_bool(capability_values.get("citations")),
            batch=_optional_bool(capability_values.get("batch")),
            code_execution=_optional_bool(capability_values.get("codeExecution")),
            request_parameters=_optional_strings(
                capability_values.get("requestParameters"), "requestParameters"
            ),
            reasoning_efforts=reasoning_efforts,
            reasoning_adaptive=_optional_bool(reasoning_values.get("adaptive")),
            reasoning_budgeted=_optional_bool(reasoning_values.get("budgeted")),
            reasoning_context_modes=_optional_strings(
                reasoning_values.get("contextModes"), "reasoning.contextModes"
            ),
            reasoning_pro_mode=_optional_bool(reasoning_values.get("proMode")),
        ),
        source=_source_from_metadata(sources, user_defined, limits.known),
        compatibility=ModelCompatibility(compatibility_raw),
        created_at=_optional_string(value.get("createdAt"), "createdAt"),
        owned_by=_optional_string(value.get("ownedBy"), "ownedBy"),
        shutdown_at=_optional_string(value.get("shutdownAt"), "shutdownAt"),
        fallback_model_ids=fallbacks,
        metadata_sources=sources,
        discovered_at=_optional_string(value.get("discoveredAt"), "discoveredAt"),
        user_defined=user_defined,
    )


def _model_document(model: ModelDescriptor) -> dict[str, object]:
    result: dict[str, object] = {"id": model.id}
    _put(result, "displayName", model.display_name)
    _put(result, "createdAt", model.created_at)
    _put(result, "ownedBy", model.owned_by)
    _put(result, "shutdownAt", model.shutdown_at)
    _put(result, "fallbackModelIds", model.fallback_model_ids)
    if model.limits.known:
        limits: dict[str, object] = {}
        _put(limits, "contextWindowTokens", model.limits.context_window_tokens)
        _put(limits, "maxInputTokens", model.limits.max_input_tokens)
        _put(limits, "maxOutputTokens", model.limits.max_output_tokens)
        result["limits"] = limits
    capabilities = _capabilities_document(model.capabilities)
    if capabilities:
        result["capabilities"] = capabilities
    if model.compatibility is not ModelCompatibility.UNKNOWN:
        result["compatibility"] = model.compatibility.value
    if model.metadata_sources:
        result["metadataSources"] = list(model.metadata_sources)
    _put(result, "discoveredAt", model.discovered_at)
    if model.user_defined:
        result["userDefined"] = True
    return result


def _capabilities_document(value: ModelCapabilities) -> dict[str, object]:
    result: dict[str, object] = {}
    _put(result, "inputModalities", value.input_modalities)
    _put(result, "outputModalities", value.output_modalities)
    _put(result, "streaming", value.streaming)
    _put(result, "toolCalling", value.tool_calling)
    _put(result, "structuredOutputs", value.structured_outputs)
    _put(result, "citations", value.citations)
    _put(result, "batch", value.batch)
    _put(result, "codeExecution", value.code_execution)
    _put(result, "contextManagement", value.context_management)
    _put(result, "requestParameters", value.request_parameters)
    reasoning: dict[str, object] = {}
    _put(reasoning, "supported", value.thinking)
    _put(reasoning, "efforts", value.reasoning_efforts)
    _put(reasoning, "adaptive", value.reasoning_adaptive)
    _put(reasoning, "budgeted", value.reasoning_budgeted)
    _put(reasoning, "contextModes", value.reasoning_context_modes)
    _put(reasoning, "proMode", value.reasoning_pro_mode)
    if reasoning:
        result["reasoning"] = reasoning
    return result


def _put(target: dict[str, object], key: str, value: object | None) -> None:
    if value is not None:
        target[key] = list(value) if isinstance(value, tuple) else value


def _optional_string(value: object, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _optional_strings(value: object, label: str) -> tuple[str, ...] | None:
    if value is None:
        return None
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item for item in value
    ):
        raise ValueError(f"{label} must be an array of non-empty strings")
    return tuple(dict.fromkeys(value))


def _optional_bool(value: object) -> bool | None:
    if value is None or isinstance(value, bool):
        return value
    raise ValueError("capability value must be boolean or null")


def _required_optional_bool(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{label} must be boolean")
    return value


def _source_from_metadata(
    sources: tuple[str, ...], user_defined: bool, has_limits: bool
) -> CapabilitySource:
    if user_defined and not sources:
        return (
            CapabilitySource.PROFILE_OVERRIDE
            if has_limits
            else CapabilitySource.FALLBACK
        )
    if CapabilitySource.BUNDLED_CATALOG.value in sources:
        return CapabilitySource.BUNDLED_CATALOG
    if CapabilitySource.CACHE.value in sources:
        return CapabilitySource.CACHE
    return CapabilitySource.PROVIDER_API


def _deduplicate_models(
    models: tuple[ModelDescriptor, ...],
) -> tuple[ModelDescriptor, ...]:
    result: list[ModelDescriptor] = []
    seen: set[str] = set()
    for model in models:
        if model.id in seen:
            continue
        seen.add(model.id)
        result.append(model)
        if len(result) == _MAX_MODELS:
            break
    return tuple(result)


def _with_limits(model: ModelDescriptor, limits: ModelLimits) -> ModelDescriptor:
    from dataclasses import replace

    return replace(model, limits=limits, source=CapabilitySource.PROFILE_OVERRIDE)


def _replace_profile_models(
    profile: ProviderProfile, models: tuple[ModelDescriptor, ...]
) -> ProviderProfile:
    from dataclasses import replace

    selected = next((model for model in models if model.id == profile.model), None)
    return replace(
        profile, limits=selected.limits if selected else profile.limits, models=models
    )


def _merge_catalog(
    preferred: tuple[ModelDescriptor, ...], retained: tuple[ModelDescriptor, ...]
) -> tuple[ModelDescriptor, ...]:
    result = list(_deduplicate_models(preferred))
    positions = {model.id: index for index, model in enumerate(result)}
    for model in retained:
        index = positions.get(model.id)
        if index is None:
            if len(result) == _MAX_MODELS:
                break
            positions[model.id] = len(result)
            result.append(model)
            continue
        current = result[index]
        if model.user_defined or model.limits.known:
            from dataclasses import replace

            result[index] = replace(
                current,
                limits=model.limits if model.limits.known else current.limits,
                source=(
                    CapabilitySource.PROFILE_OVERRIDE
                    if model.limits.known
                    else current.source
                ),
                user_defined=current.user_defined or model.user_defined,
            )
    return tuple(result)


def _legacy_cache(path: Path) -> dict[str, tuple[ModelDescriptor, ...]]:
    if not path.exists():
        return {}
    try:
        root = json.loads(path.read_text(encoding="utf-8"))
        bindings = root.get("bindings") if isinstance(root, dict) else None
        if not isinstance(bindings, dict):
            return {}
        result: dict[str, tuple[ModelDescriptor, ...]] = {}
        for key, entry in bindings.items():
            if not isinstance(key, str) or not isinstance(entry, dict):
                continue
            values = entry.get("models")
            if not isinstance(values, list):
                continue
            fetched_at = entry.get("fetchedAt")
            models: list[ModelDescriptor] = []
            for value in values[:_MAX_MODELS]:
                try:
                    model = _parse_model(value, path)
                except (ProviderProfileError, ValueError):
                    continue
                from dataclasses import replace

                models.append(
                    replace(
                        model,
                        discovered_at=fetched_at
                        if isinstance(fetched_at, str)
                        else None,
                        source=CapabilitySource.CACHE,
                    )
                )
            result[key] = tuple(models)
        return result
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}


def atomic_private_json_write(path: Path, document: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            os.chmod(temporary_path, 0o600)
            json.dump(document, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        os.chmod(path, 0o600)
    except OSError as error:
        raise ProviderProfileError(
            f"Cannot write provider profiles {path}: {error}"
        ) from error
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


__all__ = [
    "ANTHROPIC_API_BASE_URL",
    "CompactConfig",
    "ProviderProfile",
    "ProviderProfileError",
    "ProviderProfileStore",
    "ProviderProtocol",
    "OPENAI_API_BASE_URL",
    "ReasoningConfig",
    "atomic_private_json_write",
]
