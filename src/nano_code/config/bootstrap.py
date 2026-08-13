"""Idempotent creation of the user-scoped runtime layout."""

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
    """Observable initialization work, primarily for diagnostics and tests."""

    created_settings: bool
    created_providers: bool
    created_credentials: bool


def bootstrap_user_storage(paths: NanoCodePaths) -> BootstrapResult:
    """Ensure required user files exist without touching project configuration."""

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

    # Copy legacy top-level model/baseUrl into the first profile. The old fields
    # stay readable until the resolver migration lands, so startup is reversible.
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
