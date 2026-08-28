"""负责变更 provider profile 和凭据的应用服务。"""

from collections.abc import Mapping
from dataclasses import dataclass, field
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
from my_code.model.capabilities import ModelLimits
from my_code.providers.discovery import (
    ModelDiscoveryService,
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
            cached = self.model_cache.load(
                self.model_cache.binding_key(
                    profile.id, profile.protocol.value, profile.base_url
                )
            )
            models = cached.models if cached is not None else ()
            selected = next((item for item in models if item.id == profile.model), None)
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
                    capability_source=(selected.source.value if selected else None),
                    discovered_at=cached.fetched_at if cached is not None else None,
                    resolved_limits=selected.limits if selected else ModelLimits(),
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
        selected, fetched_at, discovery_error = await ModelDiscoveryService(
            self.model_cache
        ).resolve(profile, api_key=credential.api_key, timeout_seconds=10.0)
        cached = self.model_cache.load(
            self.model_cache.binding_key(
                profile.id, profile.protocol.value, profile.base_url
            )
        )
        models = cached.models if cached is not None else (selected,)
        return ProviderView(
            id=profile.id,
            protocol=profile.protocol,
            model=profile.model,
            base_url=profile.base_url,
            active=False,
            has_stored_key=self.credentials.load_api_key(profile.id) is not None,
            credential_source=credential.source,
            reasoning=profile.reasoning,
            limits=profile.limits,
            compact=profile.compact,
            models=tuple(item.id for item in models),
            capability_source=selected.source.value,
            discovered_at=fetched_at,
            discovery_error=discovery_error,
            resolved_limits=selected.limits,
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
        profile = ProviderProfile(
            id=update.id,
            protocol=update.protocol,
            model=update.model,
            base_url=update.base_url,
            reasoning=update.reasoning,
            limits=update.limits,
            compact=update.compact,
        )
        if probe_result is not None:
            if not probe_result.succeeded:
                raise ValueError("Only a successful online probe can be cached")
            if not probe_result.models or profile.model not in {
                item.id for item in probe_result.models
            }:
                raise ValueError("The selected model is not in the probed catalog")
            if probe_result.provider_id and (
                probe_result.provider_id != profile.id
                or probe_result.protocol is not profile.protocol
                or probe_result.base_url != profile.base_url
            ):
                raise ValueError("Probe result does not match the provider profile")
        profiles = self.profiles.load()
        paths = (
            self.paths.credentials_path,
            self.paths.providers_path,
            self.paths.user_settings_path,
            self.paths.model_cache_path,
        )
        snapshots = {path: _snapshot(path) for path in paths}
        try:
            if update.api_key is not None:
                self.credentials.save_api_key(update.api_key, profile.id)
            profiles[profile.id] = profile
            self.profiles.write(profiles.values())
            self.settings.set_user_active_provider(profile.id)
            if probe_result is not None:
                self.model_cache.save(
                    self.model_cache.binding_key(
                        profile.id, profile.protocol.value, profile.base_url
                    ),
                    probe_result.models,
                )
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
        return ProviderConnection(
            id=profile.id,
            protocol=profile.protocol,
            model=profile.model,
            base_url=profile.base_url,
            api_key=credential.api_key,
            credential_source=credential.source,
            reasoning=profile.reasoning,
            limits=profile.limits,
            compact=profile.compact,
        )


__all__ = [
    "ProviderManager",
    "ProviderUpdate",
    "ProviderView",
    "ProviderProbeRequest",
    "ProviderProbeResult",
]


def _snapshot(path: Path) -> bytes | None:
    return path.read_bytes() if path.exists() else None


def _restore(path: Path, content: bytes | None) -> None:
    if content is None:
        path.unlink(missing_ok=True)
    else:
        path.write_bytes(content)
