import json
from pathlib import Path

from nano_code.auth.credentials import CredentialSource, CredentialStore
from nano_code.bootstrap import initialize_user_storage
from nano_code.config.paths import NanoCodePaths, SettingsScope
from nano_code.config.providers import ProviderProfileStore, ProviderProtocol
from nano_code.config.store import SettingsStore
from nano_code.providers.manager import ProviderManager, ProviderUpdate


def make_manager(tmp_path: Path) -> tuple[ProviderManager, NanoCodePaths]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    paths = NanoCodePaths.discover(workspace, environ={}, home=tmp_path / "home")
    initialize_user_storage(paths)
    return ProviderManager(paths, environ={}), paths


def test_configure_persists_profile_key_and_active_selection(tmp_path: Path) -> None:
    manager, paths = make_manager(tmp_path)
    paths.user_settings_path.write_text(
        json.dumps({"version": 3, "futureSetting": {"enabled": True}}),
        encoding="utf-8",
    )

    connection = manager.configure(
        ProviderUpdate(
            id="company-gateway",
            model="compatible-model",
            base_url="https://gateway.example/anthropic",
            api_key="secret-key",
        )
    )

    profile = ProviderProfileStore(paths.providers_path).load()["company-gateway"]
    assert profile.model == "compatible-model"
    assert profile.base_url == "https://gateway.example/anthropic"
    assert (
        CredentialStore(paths.credentials_path).load_api_key("company-gateway")
        == "secret-key"
    )
    assert (
        SettingsStore(paths).load_scope(SettingsScope.USER).active_provider
        == "company-gateway"
    )
    assert connection.id == "company-gateway"
    assert connection.credential_source is CredentialSource.STORED
    assert json.loads(paths.user_settings_path.read_text(encoding="utf-8"))[
        "futureSetting"
    ] == {"enabled": True}


def test_blank_key_update_preserves_existing_provider_key(tmp_path: Path) -> None:
    manager, paths = make_manager(tmp_path)
    manager.configure(
        ProviderUpdate(
            id="anthropic",
            model="first-model",
            base_url=None,
            api_key="secret-key",
        )
    )

    manager.configure(
        ProviderUpdate(
            id="anthropic",
            model="second-model",
            base_url=None,
            api_key=None,
        )
    )

    assert CredentialStore(paths.credentials_path).load_api_key("anthropic") == (
        "secret-key"
    )
    assert ProviderProfileStore(paths.providers_path).load()["anthropic"].model == (
        "second-model"
    )


def test_provider_views_never_expose_key_value(tmp_path: Path) -> None:
    manager, _paths = make_manager(tmp_path)
    manager.configure(
        ProviderUpdate(
            id="anthropic",
            model="model",
            base_url=None,
            api_key="secret-key",
        )
    )

    view = manager.list("anthropic")[0]

    assert view.has_stored_key is True
    assert "secret-key" not in repr(view)


def test_openai_profile_uses_protocol_specific_environment(tmp_path: Path) -> None:
    manager, paths = make_manager(tmp_path)
    manager = ProviderManager(
        paths,
        environ={
            "OPENAI_API_KEY": "openai-key",
            "OPENAI_MODEL": "gpt-env",
            "OPENAI_BASE_URL": "https://openai.example/v1",
        },
    )

    connection = manager.configure(
        ProviderUpdate(
            id="openai",
            protocol=ProviderProtocol.OPENAI_RESPONSES,
            model="gpt-stored",
            base_url=None,
        )
    )

    assert connection.protocol is ProviderProtocol.OPENAI_RESPONSES
    assert connection.api_key == "openai-key"
    assert connection.model == "gpt-env"
    assert connection.base_url == "https://openai.example/v1"
