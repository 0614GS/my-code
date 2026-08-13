"""幂等创建用户级运行时目录结构。"""

import os
from dataclasses import dataclass

from nano_code.auth import CredentialStore
from nano_code.config.files import SettingsStore, StoredSettings
from nano_code.config.paths import NanoCodePaths, SettingsScope
from nano_code.providers.profiles import (
    DEFAULT_MODEL,
    DEFAULT_PROVIDER_ID,
    ProviderProfile,
    ProviderProfileStore,
)


@dataclass(frozen=True, slots=True)
class BootstrapResult:
    """可观察的初始化结果，主要用于诊断和测试。"""

    created_settings: bool
    created_providers: bool
    created_credentials: bool


def bootstrap_user_storage(paths: NanoCodePaths) -> BootstrapResult:
    """确保必要的用户文件存在，同时不修改项目配置。"""

    paths.config_home.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(paths.config_home, 0o700)
    paths.projects_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(paths.projects_dir, 0o700)

    settings_store = SettingsStore(paths)
    created_settings = not paths.user_settings_path.exists()
    if created_settings:
        user_settings = StoredSettings(active_provider=DEFAULT_PROVIDER_ID)
        settings_store.write(SettingsScope.USER, user_settings)
    else:
        user_settings = settings_store.load_scope(SettingsScope.USER)

    # 将旧版顶层 model/baseUrl 复制到首个 profile。旧字段在解析器迁移完成前
    # 仍保持可读，使启动过程可逆。
    default_profile = ProviderProfile(
        id=user_settings.active_provider or DEFAULT_PROVIDER_ID,
        model=user_settings.model or DEFAULT_MODEL,
        base_url=user_settings.base_url,
    )
    created_providers = ProviderProfileStore(paths.providers_path).ensure_exists(
        default_profile
    )
    created_credentials = CredentialStore(paths.credentials_path).ensure_exists()
    return BootstrapResult(
        created_settings=created_settings,
        created_providers=created_providers,
        created_credentials=created_credentials,
    )
