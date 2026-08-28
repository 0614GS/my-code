"""负责变更 provider profile 和凭据的应用服务。"""

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path

from my_code.auth.credentials import CredentialSource, CredentialStore, resolve_api_key
from my_code.config.paths import MyCodePaths
from my_code.config.providers import (
    CompactConfig,
    ProviderProfile,
    ProviderProfileStore,
    ProviderProtocol,
    ReasoningConfig,
)
from my_code.config.store import SettingsStore
from my_code.model.capabilities import (
    ModelCompatibility,
    ModelDescriptor,
    ModelLimits,
)
from my_code.providers.discovery import (
    ModelDiscoveryService,
    ProviderProbeError,
    ProviderProbeRequest,
    ProviderProbeResult,
)
from my_code.providers.model_cache import ModelCatalogCache
from my_code.providers.router import ProviderConnection


@dataclass(frozen=True, slots=True)
class ProviderView:
    """可安全暴露给终端前端且不含凭据的 profile 数据。"""

    id: str
    protocol: ProviderProtocol
    model: str
    base_url: str | None
    active: bool
    has_stored_key: bool
    credential_source: CredentialSource
    reasoning: ReasoningConfig = ReasoningConfig()
    limits: ModelLimits = ModelLimits()
    compact: CompactConfig = CompactConfig()
    models: tuple[str, ...] = ()
    capability_source: str | None = None
    discovered_at: str | None = None
    discovery_error: str | None = None
    warning: str | None = None
    resolved_limits: ModelLimits = ModelLimits()
    model_catalog: tuple["ModelView", ...] = ()


@dataclass(frozen=True, slots=True)
class ModelView:
    """Credential-free model row safe for presentation layers."""

    id: str
    display_name: str | None
    current: bool
    compatibility: ModelCompatibility
    limits: ModelLimits
    reasoning_supported: bool | None
    reasoning_efforts: tuple[str, ...] | None
    user_defined: bool

    @property
    def selectable(self) -> bool:
        return self.compatibility is not ModelCompatibility.UNSUPPORTED


@dataclass(frozen=True, slots=True)
class ProviderUpdate:
    """用户输入的 profile 数据；``api_key=None`` 表示保留现有 key。"""

    id: str
    protocol: ProviderProtocol
    model: str
    base_url: str | None
    api_key: str | None = field(default=None, repr=False)
    reasoning: ReasoningConfig = ReasoningConfig()
    limits: ModelLimits = ModelLimits()
    compact: CompactConfig = CompactConfig()
    models: tuple[ModelDescriptor, ...] = ()


class ProviderManager:
    """协调非敏感 profile、密钥及当前 provider 设置。"""

    def __init__(
        self,
        paths: MyCodePaths,
        *,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self.paths = paths
        # Kept as a compatibility-only constructor argument. Provider settings
        # and credentials deliberately never read process environment values.
        del environ
        self.profiles = ProviderProfileStore(paths.providers_path)
        self.credentials = CredentialStore(paths.credentials_path)
        self.settings = SettingsStore(paths)
        self.model_cache = ModelCatalogCache(paths.model_cache_path)

    def list(self, active_provider: str) -> tuple[ProviderView, ...]:
        views: list[ProviderView] = []
        for profile in self.profiles.load().values():
            credential = resolve_api_key(
                self.credentials,
                provider_id=profile.id,
            )
            models = profile.models
            selected = profile.selected_model
            views.append(
                ProviderView(
                    id=profile.id,
                    protocol=profile.protocol,
                    model=profile.model,
                    base_url=profile.base_url,
                    active=profile.id == active_provider,
                    has_stored_key=self.credentials.load_api_key(profile.id)
                    is not None,
                    credential_source=credential.source,
                    reasoning=profile.reasoning,
                    limits=profile.limits,
                    compact=profile.compact,
                    models=tuple(item.id for item in models),
                    capability_source=selected.source.value,
                    discovered_at=selected.discovered_at,
                    resolved_limits=selected.limits,
                    model_catalog=_model_views(profile),
                )
            )
        return tuple(views)

    async def refresh_models(self, provider_id: str) -> ProviderView:
        profiles = self.profiles.load()
        try:
            profile = profiles[provider_id]
        except KeyError as error:
            raise ValueError(f"Unknown provider: {provider_id}") from error
        credential = resolve_api_key(
            self.credentials,
            provider_id=provider_id,
        )
        result = await self.probe(
            ProviderProbeRequest(
                profile.id,
                profile.protocol,
                profile.base_url,
                credential.api_key,
            )
        )
        if not result.succeeded:
            return replace(
                self._view(profile, active=False),
                discovery_error=result.error_message,
            )
        models = _merge_discovered(profile.models, result.models)
        selected_id = _select_default(profile.model, models)
        updated = replace(
            profile,
            model=selected_id,
            limits=next(item.limits for item in models if item.id == selected_id),
            models=models,
        )
        profiles[provider_id] = updated
        self.profiles.write(profiles.values())
        selected = updated.selected_model
        return ProviderView(
            id=profile.id,
            protocol=profile.protocol,
            model=updated.model,
            base_url=profile.base_url,
            active=False,
            has_stored_key=self.credentials.load_api_key(profile.id) is not None,
            credential_source=credential.source,
            reasoning=updated.reasoning,
            limits=updated.limits,
            compact=updated.compact,
            models=tuple(item.id for item in models),
            capability_source=selected.source.value,
            discovered_at=selected.discovered_at,
            resolved_limits=selected.limits,
            model_catalog=_model_views(updated),
        )

    def _view(self, profile: ProviderProfile, *, active: bool) -> ProviderView:
        credential = resolve_api_key(self.credentials, provider_id=profile.id)
        selected = profile.selected_model
        return ProviderView(
            profile.id,
            profile.protocol,
            profile.model,
            profile.base_url,
            active,
            self.credentials.load_api_key(profile.id) is not None,
            credential.source,
            profile.reasoning,
            profile.limits,
            profile.compact,
            tuple(item.id for item in profile.models),
            selected.source.value,
            selected.discovered_at,
            resolved_limits=selected.limits,
            model_catalog=_model_views(profile),
        )

    def delete_credential(self, provider_id: str) -> bool:
        """Delete only the stored credential for an existing provider profile."""

        if provider_id not in self.profiles.load():
            raise ValueError(f"Unknown provider: {provider_id}")
        return self.credentials.delete(provider_id)

    async def probe(
        self,
        request: ProviderProbeRequest,
        *,
        timeout_seconds: float = 10.0,
    ) -> ProviderProbeResult:
        api_key = request.api_key
        if api_key is None and request.use_stored_key:
            api_key = self.credentials.load_api_key(request.provider_id)
        safe_request = ProviderProbeRequest(
            request.provider_id,
            request.protocol,
            request.base_url,
            api_key,
            request.use_stored_key,
        )
        return await ModelDiscoveryService(self.model_cache).probe(
            safe_request, timeout_seconds=timeout_seconds
        )

    def configure(
        self,
        update: ProviderUpdate,
        *,
        probe_result: ProviderProbeResult | None = None,
    ) -> ProviderConnection:
        if probe_result is not None:
            if not probe_result.succeeded:
                raise ValueError("Only a successful online probe can be persisted")
            if probe_result.provider_id and (
                probe_result.provider_id != update.id
                or probe_result.protocol is not update.protocol
                or probe_result.base_url != update.base_url
            ):
                raise ValueError("Probe result does not match the provider profile")
        existing = self.profiles.load().get(update.id)
        same_binding = (
            existing is not None
            and existing.protocol is update.protocol
            and _normalized_url(existing.base_url) == _normalized_url(update.base_url)
        )
        if probe_result is not None:
            catalog = _merge_discovered(
                existing.models if same_binding and existing is not None else (),
                probe_result.models,
            )
        elif update.models:
            catalog = _manual_catalog(update.models)
            if same_binding and existing is not None:
                catalog = _merge_manual(existing.models, catalog)
        elif same_binding and existing is not None:
            catalog = _merge_manual(
                existing.models,
                _manual_catalog((ModelDescriptor(update.model),)),
            )
        else:
            catalog = _manual_catalog((ModelDescriptor(update.model),))
        model_id = _select_default(update.model, catalog)
        profile = ProviderProfile(
            id=update.id,
            protocol=update.protocol,
            model=model_id,
            base_url=update.base_url,
            reasoning=update.reasoning,
            limits=(
                update.limits
                if update.limits.known
                else next(item.limits for item in catalog if item.id == model_id)
            ),
            compact=update.compact,
            models=catalog,
        )
        profiles = self.profiles.load()
        paths = (
            self.paths.credentials_path,
            self.paths.providers_path,
            self.paths.user_settings_path,
        )
        snapshots = {path: _snapshot(path) for path in paths}
        try:
            if update.api_key is not None:
                self.credentials.save_api_key(update.api_key, profile.id)
            profiles[profile.id] = profile
            self.profiles.write(profiles.values())
            self.settings.set_user_active_provider(profile.id)
        except Exception:
            for path, snapshot in snapshots.items():
                _restore(path, snapshot)
            raise
        return self.resolve(profile.id)

    def select_provider(self, provider_id: str) -> ProviderConnection:
        """Persist selection of an existing profile without rewriting it."""

        connection = self.resolve(provider_id)
        self.settings.set_user_active_provider(provider_id)
        return connection

    def select_model(self, provider_id: str, model_id: str) -> ProviderConnection:
        """Persist a model from the profile's local catalog only."""

        profiles = self.profiles.load()
        try:
            profile = profiles[provider_id]
        except KeyError as error:
            raise ValueError(f"Unknown provider: {provider_id}") from error
        selected = next((item for item in profile.models if item.id == model_id), None)
        if selected is None:
            raise ValueError("Model is not in the provider's local catalog")
        if not selected.selectable:
            raise ValueError("Model is incompatible with the provider protocol")
        profiles[provider_id] = replace(
            profile,
            model=model_id,
            limits=selected.limits,
        )
        self.profiles.write(profiles.values())
        return self.resolve(provider_id)

    def resolve(self, provider_id: str) -> ProviderConnection:
        profiles = self.profiles.load()
        try:
            profile = profiles[provider_id]
        except KeyError as error:
            raise ValueError(f"Unknown provider: {provider_id}") from error
        credential = resolve_api_key(
            self.credentials,
            provider_id=provider_id,
        )
        reasoning, warning = effective_reasoning(
            profile.reasoning, profile.selected_model
        )
        return ProviderConnection(
            id=profile.id,
            protocol=profile.protocol,
            model=profile.model,
            base_url=profile.base_url,
            api_key=credential.api_key,
            credential_source=credential.source,
            reasoning=reasoning,
            limits=profile.limits,
            compact=profile.compact,
            model_descriptor=profile.selected_model,
            warning=warning,
        )


__all__ = [
    "ProviderManager",
    "ModelView",
    "ProviderUpdate",
    "ProviderView",
    "effective_reasoning",
    "ProviderProbeRequest",
    "ProviderProbeError",
    "ProviderProbeResult",
]


def _snapshot(path: Path) -> bytes | None:
    return path.read_bytes() if path.exists() else None


def _restore(path: Path, content: bytes | None) -> None:
    if content is None:
        path.unlink(missing_ok=True)
    else:
        path.write_bytes(content)


def _normalized_url(value: str | None) -> str:
    return (value or "<sdk-default>").rstrip("/").casefold()


def _manual_catalog(models: tuple[ModelDescriptor, ...]) -> tuple[ModelDescriptor, ...]:
    result: list[ModelDescriptor] = []
    seen: set[str] = set()
    for item in models:
        model_id = item.id.strip()
        if not model_id or model_id in seen:
            continue
        seen.add(model_id)
        result.append(
            replace(
                item,
                id=model_id,
                display_name=item.display_name or model_id,
                compatibility=ModelCompatibility.UNKNOWN,
                user_defined=True,
            )
        )
        if len(result) == 1_000:
            break
    if not result:
        raise ValueError("At least one model ID is required")
    return tuple(result)


def _merge_manual(
    existing: tuple[ModelDescriptor, ...], manual: tuple[ModelDescriptor, ...]
) -> tuple[ModelDescriptor, ...]:
    result = list(existing)
    positions = {item.id: index for index, item in enumerate(result)}
    for item in manual:
        index = positions.get(item.id)
        if index is None:
            if len(result) == 1_000:
                break
            positions[item.id] = len(result)
            result.append(item)
        else:
            result[index] = replace(result[index], user_defined=True)
    return tuple(result)


def _merge_discovered(
    existing: tuple[ModelDescriptor, ...], discovered: tuple[ModelDescriptor, ...]
) -> tuple[ModelDescriptor, ...]:
    manual = {item.id: item for item in existing if item.user_defined}
    result: list[ModelDescriptor] = []
    seen: set[str] = set()
    for item in discovered:
        if item.id in seen:
            continue
        seen.add(item.id)
        result.append(replace(item, user_defined=item.id in manual))
        if len(result) == 1_000:
            return tuple(result)
    for item in existing:
        if not item.user_defined or item.id in seen:
            continue
        result.append(item)
        if len(result) == 1_000:
            break
    return tuple(result)


def _select_default(preferred: str, models: tuple[ModelDescriptor, ...]) -> str:
    preferred_model = next((item for item in models if item.id == preferred), None)
    if preferred_model is not None and preferred_model.selectable:
        return preferred
    selectable = next((item for item in models if item.selectable), None)
    if selectable is None:
        raise ValueError("The provider catalog has no compatible models")
    return selectable.id


def _model_views(profile: ProviderProfile) -> tuple[ModelView, ...]:
    return tuple(
        ModelView(
            item.id,
            item.display_name,
            item.id == profile.model,
            item.compatibility,
            item.limits,
            item.capabilities.thinking,
            item.capabilities.reasoning_efforts,
            item.user_defined,
        )
        for item in profile.models
        if item.selectable
    )


def effective_reasoning(
    configured: ReasoningConfig, model: ModelDescriptor
) -> tuple[ReasoningConfig, str | None]:
    capabilities = model.capabilities
    if capabilities.thinking is False and configured.enabled:
        return replace(
            configured, enabled=False
        ), "Reasoning is unavailable for this model."
    effort = configured.effort
    warning: str | None = None
    if effort != "auto" and (
        capabilities.effort is False
        or (
            capabilities.reasoning_efforts is not None
            and effort not in capabilities.reasoning_efforts
        )
    ):
        effort = "auto"
        warning = "Configured reasoning effort is unavailable; using auto."
    context = configured.context
    if context != "auto" and (
        capabilities.context_management is False
        or (
            capabilities.reasoning_context_modes is not None
            and context not in capabilities.reasoning_context_modes
        )
    ):
        context = "auto"
        warning = "Configured reasoning context is unavailable; using auto."
    return replace(configured, effort=effort, context=context), warning
