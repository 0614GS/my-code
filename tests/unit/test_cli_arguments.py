import json
from pathlib import Path

import pytest

from nano_code.auth import CredentialSource, CredentialStore
from nano_code.cli.arguments import AuthAction, AuthOptions, parse_args, parse_cli
from nano_code.permissions import PermissionMode


def clear_provider_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_MODEL",
        "NANO_CODE_API_KEY",
        "NANO_CODE_PROVIDER",
    ):
        monkeypatch.delenv(name, raising=False)


def test_cli_resolves_file_environment_and_flag_precedence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clear_provider_environment(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config_home = tmp_path / "config"
    config_home.mkdir()
    (config_home / "settings.json").write_text(
        json.dumps(
            {
                "model": "file-model",
                "baseUrl": "https://file.example/api",
                "permissions": {"defaultMode": "plan"},
                "maxTurns": 3,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("NANO_CODE_CONFIG_DIR", str(config_home))
    monkeypatch.setenv("ANTHROPIC_MODEL", "env-model")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://env.example/api")

    options = parse_args(
        [
            "--cwd",
            str(workspace),
            "--model",
            "cli-model",
            "--base-url",
            "https://cli.example/api",
            "--max-turns",
            "7",
            "-p",
            "hello",
        ]
    )

    assert options.settings.model == "cli-model"
    assert options.settings.base_url == "https://cli.example/api"
    assert options.settings.permission_mode is PermissionMode.PLAN
    assert options.settings.max_turns == 7
    assert options.settings.paths.config_home == config_home


def test_environment_model_overrides_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clear_provider_environment(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config_home = tmp_path / "config"
    config_home.mkdir()
    (config_home / "settings.json").write_text(
        json.dumps({"model": "file-model"}), encoding="utf-8"
    )
    monkeypatch.setenv("NANO_CODE_CONFIG_DIR", str(config_home))
    monkeypatch.setenv("ANTHROPIC_MODEL", "env-model")

    options = parse_args(["--cwd", str(workspace), "-p", "hello"])

    assert options.settings.model == "env-model"


def test_environment_base_url_overrides_user_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clear_provider_environment(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config_home = tmp_path / "config"
    config_home.mkdir()
    (config_home / "settings.json").write_text(
        json.dumps({"baseUrl": "https://file.example/api"}), encoding="utf-8"
    )
    monkeypatch.setenv("NANO_CODE_CONFIG_DIR", str(config_home))
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://env.example/api")

    options = parse_args(["--cwd", str(workspace), "-p", "hello"])

    assert options.settings.base_url == "https://env.example/api"


def test_named_provider_resolves_profile_and_scoped_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clear_provider_environment(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config_home = tmp_path / "config"
    config_home.mkdir()
    (config_home / "settings.json").write_text(
        json.dumps({"activeProvider": "gateway"}), encoding="utf-8"
    )
    (config_home / "providers.json").write_text(
        json.dumps(
            {
                "version": 1,
                "providers": {
                    "gateway": {
                        "protocol": "anthropic-messages",
                        "model": "gateway-model",
                        "baseUrl": "https://gateway.example/api",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    CredentialStore(config_home / ".credentials.json").save_api_key(
        "gateway-key", "gateway"
    )
    monkeypatch.setenv("NANO_CODE_CONFIG_DIR", str(config_home))

    options = parse_args(["--cwd", str(workspace), "-p", "hello"])

    assert options.settings.provider_id == "gateway"
    assert options.settings.model == "gateway-model"
    assert options.settings.base_url == "https://gateway.example/api"
    assert options.settings.api_key == "gateway-key"


def test_cli_provider_override_selects_named_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clear_provider_environment(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config_home = tmp_path / "config"
    config_home.mkdir()
    (config_home / "providers.json").write_text(
        json.dumps(
            {
                "version": 1,
                "providers": {
                    "anthropic": {
                        "protocol": "anthropic-messages",
                        "model": "default-model",
                    },
                    "gateway": {
                        "protocol": "anthropic-messages",
                        "model": "gateway-model",
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("NANO_CODE_CONFIG_DIR", str(config_home))

    options = parse_args(
        ["--cwd", str(workspace), "--provider", "gateway", "-p", "hello"]
    )

    assert options.settings.provider_id == "gateway"
    assert options.settings.model == "gateway-model"


def test_environment_api_key_overrides_stored_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clear_provider_environment(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config_home = tmp_path / "config"
    monkeypatch.setenv("NANO_CODE_CONFIG_DIR", str(config_home))
    CredentialStore(config_home / ".credentials.json").save_api_key("stored-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "environment-key")

    options = parse_args(["--cwd", str(workspace), "-p", "hello"])

    assert options.settings.api_key == "environment-key"
    assert options.settings.credential_source is CredentialSource.ENVIRONMENT


def test_cli_uses_stored_credential_without_environment_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clear_provider_environment(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config_home = tmp_path / "config"
    monkeypatch.setenv("NANO_CODE_CONFIG_DIR", str(config_home))
    CredentialStore(config_home / ".credentials.json").save_api_key("stored-key")

    options = parse_args(["--cwd", str(workspace), "-p", "hello"])

    assert options.settings.api_key == "stored-key"
    assert options.settings.credential_source is CredentialSource.STORED


def test_parse_cli_returns_auth_management_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NANO_CODE_CONFIG_DIR", str(tmp_path / "config"))

    options = parse_cli(["auth", "login"])

    assert isinstance(options, AuthOptions)
    assert options.action is AuthAction.LOGIN
    assert options.paths.config_home == tmp_path / "config"


def test_parsing_does_not_materialize_storage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clear_provider_environment(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config_home = tmp_path / "missing-config"
    monkeypatch.setenv("NANO_CODE_CONFIG_DIR", str(config_home))

    options = parse_args(["--cwd", str(workspace), "-p", "hello"])

    assert options.settings.paths.config_home == config_home
    assert not config_home.exists()
    assert not (workspace / ".nano-code").exists()


def test_non_positive_cli_limit_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clear_provider_environment(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("NANO_CODE_CONFIG_DIR", str(tmp_path / "config"))

    with pytest.raises(ValueError, match="max_turns must be a positive integer"):
        parse_args(["--cwd", str(workspace), "--max-turns", "0", "-p", "hello"])
