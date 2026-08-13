import json
import stat
from pathlib import Path

import pytest

from nano_code.auth import CredentialStore
from nano_code.cli.main import main
from nano_code.config import NanoCodePaths, bootstrap_user_storage
from nano_code.providers.profiles import ProviderProfileStore


def make_paths(tmp_path: Path) -> NanoCodePaths:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return NanoCodePaths.discover(workspace, environ={}, home=tmp_path / "home")


def test_bootstrap_creates_required_user_layout_only(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)

    result = bootstrap_user_storage(paths)

    assert result.created_settings is True
    assert result.created_providers is True
    assert result.created_credentials is True
    assert json.loads(paths.user_settings_path.read_text(encoding="utf-8")) == {
        "version": 1,
        "activeProvider": "anthropic",
    }
    assert set(ProviderProfileStore(paths.providers_path).load()) == {"anthropic"}
    assert CredentialStore(paths.credentials_path).load_api_key() is None
    assert paths.projects_dir.is_dir()
    assert not paths.project_settings_path.exists()
    assert stat.S_IMODE(paths.config_home.stat().st_mode) == 0o700
    assert stat.S_IMODE(paths.credentials_path.stat().st_mode) == 0o600


def test_bootstrap_is_idempotent_and_preserves_existing_profiles(
    tmp_path: Path,
) -> None:
    paths = make_paths(tmp_path)
    bootstrap_user_storage(paths)
    original = paths.providers_path.read_text(encoding="utf-8")

    result = bootstrap_user_storage(paths)

    assert result.created_settings is False
    assert result.created_providers is False
    assert result.created_credentials is False
    assert paths.providers_path.read_text(encoding="utf-8") == original


def test_bootstrap_migrates_legacy_credential_and_provider_fields(
    tmp_path: Path,
) -> None:
    paths = make_paths(tmp_path)
    paths.config_home.mkdir(parents=True)
    paths.user_settings_path.write_text(
        json.dumps(
            {
                "model": "legacy-model",
                "baseUrl": "https://legacy.example/api",
            }
        ),
        encoding="utf-8",
    )
    paths.credentials_path.write_text(
        json.dumps({"anthropicApiKey": "legacy-key"}), encoding="utf-8"
    )

    bootstrap_user_storage(paths)

    profile = ProviderProfileStore(paths.providers_path).load()["anthropic"]
    assert profile.model == "legacy-model"
    assert profile.base_url == "https://legacy.example/api"
    assert CredentialStore(paths.credentials_path).load_api_key() == "legacy-key"
    assert "anthropicApiKey" not in paths.credentials_path.read_text(encoding="utf-8")


def test_cli_startup_bootstraps_before_auth_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config_home = tmp_path / "config"
    monkeypatch.chdir(workspace)
    monkeypatch.setenv("NANO_CODE_CONFIG_DIR", str(config_home))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    with pytest.raises(SystemExit) as exit_info:
        main(["auth", "status"])

    assert exit_info.value.code == 1
    assert (config_home / "settings.json").exists()
    assert (config_home / "providers.json").exists()
    assert (config_home / ".credentials.json").exists()
