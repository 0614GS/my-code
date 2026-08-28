import json
from pathlib import Path

import pytest

from my_code.auth.credentials import CredentialSource, CredentialStore
from my_code.bootstrap import initialize_user_storage
from my_code.config.paths import MyCodePaths, SettingsScope
from my_code.config.providers import ProviderProfileStore, ProviderProtocol
from my_code.config.store import SettingsStore
from my_code.providers.manager import ProviderManager, ProviderUpdate


def make_manager(tmp_path: Path) -> tuple[ProviderManager, MyCodePaths]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    paths = MyCodePaths.discover(workspace, environ={}, home=tmp_path / "home")
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
            protocol=ProviderProtocol.ANTHROPIC_MESSAGES,
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
            protocol=ProviderProtocol.ANTHROPIC_MESSAGES,
            model="first-model",
            base_url=None,
            api_key="secret-key",
        )
    )

    manager.configure(
        ProviderUpdate(
            id="anthropic",
            protocol=ProviderProtocol.ANTHROPIC_MESSAGES,
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
            protocol=ProviderProtocol.ANTHROPIC_MESSAGES,
            model="model",
            base_url=None,
            api_key="secret-key",
        )
    )

    view = manager.list("anthropic")[0]

    assert view.has_stored_key is True
    assert view.credential_source is CredentialSource.STORED
    assert "secret-key" not in repr(view)


def test_provider_view_ignores_environment_credentials(
    tmp_path: Path,
) -> None:
    original_manager, paths = make_manager(tmp_path)
    original_manager.configure(
        ProviderUpdate(
            id="anthropic",
            protocol=ProviderProtocol.ANTHROPIC_MESSAGES,
            model="model",
            base_url=None,
        )
    )
    manager = ProviderManager(paths, environ={"ANTHROPIC_API_KEY": "environment-key"})

    view = manager.list("anthropic")[0]

    assert view.credential_source is CredentialSource.NONE
    assert view.has_stored_key is False
    assert "environment-key" not in repr(view)


def test_delete_credential_only_removes_the_target_profile(tmp_path: Path) -> None:
    manager, paths = make_manager(tmp_path)
    manager.configure(
        ProviderUpdate(
            "anthropic",
            ProviderProtocol.ANTHROPIC_MESSAGES,
            "model-a",
            None,
            "key-a",
        )
    )
    manager.configure(
        ProviderUpdate(
            "other",
            ProviderProtocol.ANTHROPIC_MESSAGES,
            "model-b",
            None,
            "key-b",
        )
    )

    assert manager.delete_credential("anthropic") is True
    assert manager.delete_credential("anthropic") is False
    assert CredentialStore(paths.credentials_path).load_api_key("anthropic") is None
    assert CredentialStore(paths.credentials_path).load_api_key("other") == "key-b"


def test_delete_credential_rejects_unknown_provider(tmp_path: Path) -> None:
    manager, _paths = make_manager(tmp_path)

    with pytest.raises(ValueError, match="Unknown provider"):
        manager.delete_credential("missing")


def test_openai_profile_ignores_protocol_specific_environment(tmp_path: Path) -> None:
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
    assert connection.api_key is None
    assert connection.model == "gpt-stored"
    assert connection.base_url is None
