"""负责变更 provider profile 和凭据的应用服务。"""

import os
from collections.abc import Mapping
from dataclasses import dataclass

from nano_code.auth import CredentialStore, resolve_api_key
from nano_code.core.paths import NanoCodePaths
from nano_code.core.settings_store import SettingsStore
from nano_code.providers.profiles import (
    ProviderProfile,
    ProviderProfileStore,
    ProviderProtocol,
)
from nano_code.providers.router import ProviderConnection


@dataclass(frozen=True, slots=True)
class ProviderView:
    """可安全暴露给终端前端且不含凭据的 profile 数据。"""

    id: str
    protocol: ProviderProtocol
    model: str
    base_url: str | None
    active: bool
    has_stored_key: bool


@dataclass(frozen=True, slots=True)
class ProviderUpdate:
    """用户输入的 profile 数据；``api_key=None`` 表示保留现有 key。"""

    id: str
    model: str
    base_url: str | None
    api_key: str | None = None


class ProviderManager:
    """协调非敏感 profile、密钥及当前 provider 设置。"""

    def __init__(
        self,
        paths: NanoCodePaths,
        *,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self.paths = paths
        self.environ = os.environ if environ is None else environ
        self.profiles = ProviderProfileStore(paths.providers_path)
        self.credentials = CredentialStore(paths.credentials_path)
        self.settings = SettingsStore(paths)

    def list(self, active_provider: str) -> tuple[ProviderView, ...]:
        return tuple(
            ProviderView(
                id=profile.id,
                protocol=profile.protocol,
                model=profile.model,
                base_url=profile.base_url,
                active=profile.id == active_provider,
                has_stored_key=self.credentials.load_api_key(profile.id) is not None,
            )
            for profile in self.profiles.load().values()
        )

    def configure(self, update: ProviderUpdate) -> ProviderConnection:
        profile = ProviderProfile(
            id=update.id,
            protocol=ProviderProtocol.ANTHROPIC_MESSAGES,
            model=update.model,
            base_url=update.base_url,
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
        )
        return ProviderConnection(
            id=profile.id,
            protocol=profile.protocol,
            model=self.environ.get("ANTHROPIC_MODEL") or profile.model,
            base_url=self.environ.get("ANTHROPIC_BASE_URL") or profile.base_url,
            api_key=credential.api_key,
            credential_source=credential.source,
        )
