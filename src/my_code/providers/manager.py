"""负责变更 provider profile 和凭据的应用服务。"""

import os
from collections.abc import Mapping
from dataclasses import dataclass

from my_code.auth.credentials import CredentialStore, resolve_api_key
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
from my_code.providers.discovery import ModelDiscoveryService
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
    model: str
    base_url: str | None
    api_key: str | None = None
    protocol: ProviderProtocol = ProviderProtocol.ANTHROPIC_MESSAGES
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
        self.environ = os.environ if environ is None else environ
        self.profiles = ProviderProfileStore(paths.providers_path)
        self.credentials = CredentialStore(paths.credentials_path)
        self.settings = SettingsStore(paths)
        self.model_cache = ModelCatalogCache(paths.model_cache_path)

    def list(self, active_provider: str) -> tuple[ProviderView, ...]:
        views: list[ProviderView] = []
        for profile in self.profiles.load().values():
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
            self.environ,
            provider_id=provider_id,
            protocol=profile.protocol.value,
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
            profile.id,
            profile.protocol,
            profile.model,
            profile.base_url,
            False,
            credential.api_key is not None,
            profile.reasoning,
            profile.limits,
            profile.compact,
            tuple(item.id for item in models),
            selected.source.value,
            fetched_at,
            discovery_error,
            None,
            selected.limits,
        )

    def configure(self, update: ProviderUpdate) -> ProviderConnection:
        profile = ProviderProfile(
            id=update.id,
            protocol=update.protocol,
            model=update.model,
            base_url=update.base_url,
            reasoning=update.reasoning,
            limits=update.limits,
            compact=update.compact,
        )
        profiles = self.profiles.load()

        # 先持久化密钥。如果后续 profile 写入失败，只会留下无法访问的孤立 key，
        # 而不会生成缺少 key 的可用路由。
        if update.api_key is not None:
            self.credentials.save_api_key(update.api_key, profile.id)
        profiles[profile.id] = profile
        self.profiles.write(profiles.values())

        self.settings.set_user_active_provider(profile.id)
        return self.resolve(profile.id)

    def resolve(self, provider_id: str) -> ProviderConnection:
        profiles = self.profiles.load()
        try:
            profile = profiles[provider_id]
        except KeyError as error:
            raise ValueError(f"Unknown provider: {provider_id}") from error
        credential = resolve_api_key(
            self.credentials,
            self.environ,
            provider_id=provider_id,
            protocol=profile.protocol.value,
        )
        prefix = (
            "OPENAI"
            if profile.protocol is ProviderProtocol.OPENAI_RESPONSES
            else "ANTHROPIC"
        )
        return ProviderConnection(
            id=profile.id,
            protocol=profile.protocol,
            model=self.environ.get(f"{prefix}_MODEL") or profile.model,
            base_url=self.environ.get(f"{prefix}_BASE_URL") or profile.base_url,
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
]
