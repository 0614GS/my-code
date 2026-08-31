import json
from pathlib import Path

import pytest

from my_code.auth.credentials import CredentialSource, CredentialStore
from my_code.cli.arguments import (
    CliOptions,
    build_parser,
    parse_args,
    parse_cli,
)
from my_code.config.settings import AgentSettings, SettingsResolver
from my_code.permissions.models import PermissionMode


def clear_provider_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_MODEL",
        "MY_CODE_API_KEY",
        "MY_CODE_PROVIDER",
    ):
        monkeypatch.delenv(name, raising=False)


def resolve_options(options: CliOptions) -> AgentSettings:
    resolver = SettingsResolver.for_workspace(options.cwd)
    return resolver.resolve(
        options.settings_overrides,
        interactive=True,
    )


def write_provider_config(
    config_home: Path,
    *,
    model: str = "profile-model",
    base_url: str | None = "https://profile.example/api",
) -> None:
    config_home.mkdir(parents=True, exist_ok=True)
    (config_home / "settings.json").write_text(
        json.dumps({"version": 3, "activeProvider": "anthropic"}),
        encoding="utf-8",
    )
    profile: dict[str, object] = {
        "protocol": "anthropic-messages",
        "defaultModel": model,
    }
    if base_url is not None:
        profile["baseUrl"] = base_url
    (config_home / "providers.json").write_text(
        json.dumps({"version": 3, "providers": {"anthropic": profile}}),
        encoding="utf-8",
    )


def test_parser_uses_installed_command_name() -> None:
    assert build_parser().prog == "mycode"


def test_parser_rejects_removed_max_turns_flag() -> None:
    with pytest.raises(SystemExit):
        parse_args(["--max-turns", "3"])


def test_parser_rejects_removed_context_chars_flag() -> None:
    with pytest.raises(SystemExit):
        parse_args(["--context-chars", "1000"])


@pytest.mark.parametrize(
    "arguments",
    (("-p", "hello"), ("--print", "hello"), ("hello",)),
)
def test_parser_rejects_non_interactive_chat_forms(
    arguments: tuple[str, ...],
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        parse_cli(list(arguments))

    assert exit_info.value.code == 2


def test_cli_resolves_profile_and_non_provider_flag_precedence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clear_provider_environment(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config_home = tmp_path / "config"
    write_provider_config(config_home)
    (config_home / "settings.json").write_text(
        json.dumps(
            {
                "version": 3,
                "activeProvider": "anthropic",
                "agent": {"maxSteps": 3},
                "permissions": {"defaultMode": "plan"},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("MY_CODE_CONFIG_DIR", str(config_home))
    monkeypatch.setenv("ANTHROPIC_MODEL", "env-model")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://env.example/api")

    options = parse_args(
        [
            "--cwd",
            str(workspace),
            "--max-steps",
            "7",
        ]
    )

    settings = resolve_options(options)
    assert settings.model == "profile-model"
    assert settings.base_url == "https://profile.example/api"
    assert settings.permission_mode is PermissionMode.PLAN
    assert settings.max_steps == 7
    assert settings.paths.config_home == config_home


def test_agent_model_returns_migration_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clear_provider_environment(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config_home = tmp_path / "config"
    config_home.mkdir()
    (config_home / "settings.json").write_text(
        json.dumps({"version": 3, "agent": {"model": "file-model"}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("MY_CODE_CONFIG_DIR", str(config_home))
    write_provider_config(config_home)
    (config_home / "settings.json").write_text(
        json.dumps(
            {
                "version": 3,
                "activeProvider": "anthropic",
                "agent": {"model": "file-model"},
            }
        ),
        encoding="utf-8",
    )

    options = parse_args(["--cwd", str(workspace)])

    with pytest.raises(ValueError, match="defaultModel"):
        resolve_options(options)


def test_provider_environment_cannot_override_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clear_provider_environment(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config_home = tmp_path / "config"
    write_provider_config(config_home)
    monkeypatch.setenv("MY_CODE_CONFIG_DIR", str(config_home))
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://env.example/api")

    options = parse_args(["--cwd", str(workspace)])

    monkeypatch.setenv("ANTHROPIC_MODEL", "env-model")
    monkeypatch.setenv("MY_CODE_PROVIDER", "missing")
    settings = resolve_options(options)
    assert settings.base_url == "https://profile.example/api"
    assert settings.model == "profile-model"
    assert settings.provider_id == "anthropic"


def test_named_provider_resolves_profile_and_scoped_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clear_provider_environment(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config_home = tmp_path / "config"
    config_home.mkdir()
    (config_home / "settings.json").write_text(
        json.dumps({"version": 3, "activeProvider": "gateway"}), encoding="utf-8"
    )
    (config_home / "providers.json").write_text(
        json.dumps(
            {
                "version": 2,
                "providers": {
                    "gateway": {
                        "protocol": "anthropic-messages",
                        "defaultModel": "gateway-model",
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
    monkeypatch.setenv("MY_CODE_CONFIG_DIR", str(config_home))

    options = parse_args(["--cwd", str(workspace)])

    settings = resolve_options(options)
    assert settings.provider_id == "gateway"
    assert settings.model == "gateway-model"
    assert settings.base_url == "https://gateway.example/api"
    assert settings.api_key == "gateway-key"


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
                "version": 2,
                "providers": {
                    "anthropic": {
                        "protocol": "anthropic-messages",
                        "defaultModel": "default-model",
                    },
                    "gateway": {
                        "protocol": "anthropic-messages",
                        "defaultModel": "gateway-model",
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("MY_CODE_CONFIG_DIR", str(config_home))

    options = parse_args(["--cwd", str(workspace), "--provider", "gateway"])

    settings = resolve_options(options)
    assert settings.provider_id == "gateway"
    assert settings.model == "gateway-model"


def test_environment_api_key_cannot_override_stored_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clear_provider_environment(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config_home = tmp_path / "config"
    write_provider_config(config_home)
    monkeypatch.setenv("MY_CODE_CONFIG_DIR", str(config_home))
    CredentialStore(config_home / ".credentials.json").save_api_key(
        "stored-key", "anthropic"
    )
    monkeypatch.setenv("ANTHROPIC_API_KEY", "environment-key")

    options = parse_args(["--cwd", str(workspace)])

    settings = resolve_options(options)
    assert settings.api_key == "stored-key"
    assert settings.credential_source is CredentialSource.STORED


def test_cli_uses_stored_credential_without_environment_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clear_provider_environment(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config_home = tmp_path / "config"
    write_provider_config(config_home)
    monkeypatch.setenv("MY_CODE_CONFIG_DIR", str(config_home))
    CredentialStore(config_home / ".credentials.json").save_api_key(
        "stored-key", "anthropic"
    )

    options = parse_args(["--cwd", str(workspace)])

    settings = resolve_options(options)
    assert settings.api_key == "stored-key"
    assert settings.credential_source is CredentialSource.STORED


def test_session_and_launch_overrides_are_preserved(tmp_path: Path) -> None:
    options = parse_cli(
        [
            "--cwd",
            str(tmp_path),
            "--session",
            "11111111-1111-1111-1111-111111111111",
            "--provider",
            "gateway",
        ]
    )

    assert options.cwd == tmp_path
    assert options.session_id == "11111111-1111-1111-1111-111111111111"
    assert options.settings_overrides.provider_id == "gateway"


@pytest.mark.parametrize("flag", ("--model", "--base-url"))
def test_parser_rejects_removed_provider_overrides(flag: str) -> None:
    with pytest.raises(SystemExit):
        parse_cli([flag, "removed"])


def test_parsing_does_not_materialize_storage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clear_provider_environment(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config_home = tmp_path / "missing-config"
    monkeypatch.setenv("MY_CODE_CONFIG_DIR", str(config_home))

    options = parse_args(["--cwd", str(workspace)])

    assert options.cwd == workspace
    assert not config_home.exists()
    assert not (workspace / ".my-code").exists()


def test_non_positive_cli_limit_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clear_provider_environment(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config_home = tmp_path / "config"
    write_provider_config(config_home)
    monkeypatch.setenv("MY_CODE_CONFIG_DIR", str(config_home))

    options = parse_args(["--cwd", str(workspace), "--max-steps", "0"])
    with pytest.raises(ValueError, match="max_steps must be a positive integer"):
        resolve_options(options)


def test_max_steps_is_unlimited_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clear_provider_environment(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config_home = tmp_path / "config"
    write_provider_config(config_home)
    monkeypatch.setenv("MY_CODE_CONFIG_DIR", str(config_home))

    options = parse_args(["--cwd", str(workspace)])

    assert resolve_options(options).max_steps is None
