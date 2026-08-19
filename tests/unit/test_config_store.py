"""Layered configuration store tests."""

import json
import stat
from pathlib import Path

import pytest

from nano_code.config.paths import NanoCodePaths, SettingsScope
from nano_code.config.settings import SettingsResolver
from nano_code.config.store import SettingsFileError, SettingsLayer, SettingsStore
from nano_code.permissions.models import (
    PermissionBehavior,
    PermissionMode,
    PermissionRule,
)


def make_paths(tmp_path: Path) -> NanoCodePaths:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return NanoCodePaths(cwd=workspace.resolve(), config_home=tmp_path / "state")


def test_load_merges_user_project_and_local_precedence(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    store = SettingsStore(paths)
    store.write(
        SettingsScope.USER,
        SettingsLayer(
            model="user-model",
            permission_mode=PermissionMode.PLAN,
            max_steps=10,
        ),
    )
    store.write(
        SettingsScope.PROJECT,
        SettingsLayer(model="project-model", max_output_tokens=4000),
    )
    store.write(
        SettingsScope.LOCAL,
        SettingsLayer(
            permission_mode=PermissionMode.ACCEPT_EDITS,
            max_steps=20,
        ),
    )

    assert store.load() == SettingsLayer(
        model="project-model",
        permission_mode=PermissionMode.ACCEPT_EDITS,
        max_steps=20,
        max_output_tokens=4000,
    )
    user_document = json.loads(paths.user_settings_path.read_text(encoding="utf-8"))
    assert user_document["agent"]["maxSteps"] == 10
    assert "maxTurns" not in user_document["agent"]


def test_empty_and_missing_files_are_empty_layers(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    paths.project_settings_path.parent.mkdir()
    paths.project_settings_path.write_text("\n", encoding="utf-8")

    assert SettingsStore(paths).load() == SettingsLayer()


def test_project_layers_are_skipped_when_started_from_user_home(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    paths = NanoCodePaths(cwd=home, config_home=home / ".nano-code")
    store = SettingsStore(paths)
    store.write(
        SettingsScope.USER,
        SettingsLayer(active_provider="gateway", model="user-model"),
    )
    paths.local_settings_path.write_text(
        json.dumps({"version": 3, "agent": {"model": "misclassified-local-model"}}),
        encoding="utf-8",
    )

    assert store.load_scope(SettingsScope.PROJECT) == SettingsLayer()
    assert store.load_scope(SettingsScope.LOCAL) == SettingsLayer()
    assert store.load() == SettingsLayer(active_provider="gateway", model="user-model")


@pytest.mark.parametrize("scope", [SettingsScope.PROJECT, SettingsScope.LOCAL])
def test_project_writes_cannot_overwrite_colliding_user_storage(
    tmp_path: Path, scope: SettingsScope
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    paths = NanoCodePaths(cwd=home, config_home=home / ".nano-code")

    with pytest.raises(SettingsFileError, match="project config directory"):
        SettingsStore(paths).write(scope, SettingsLayer(model="project-model"))


def test_unknown_keys_are_ignored_for_forward_compatibility(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    paths.user_settings_path.parent.mkdir()
    paths.user_settings_path.write_text(
        json.dumps(
            {
                "version": 3,
                "agent": {"model": "known"},
                "futureSetting": {"enabled": True},
            }
        ),
        encoding="utf-8",
    )

    assert SettingsStore(paths).load().model == "known"


@pytest.mark.parametrize(
    "document, message",
    [
        ([], "root must be an object"),
        (
            {"version": 3, "agent": {"maxSteps": True}},
            "agent.maxSteps must be a positive integer",
        ),
        (
            {"version": 3, "agent": {"maxTurns": 3}},
            "agent.maxTurns is no longer supported",
        ),
        ({"version": 3, "permissions": []}, "permissions must be an object"),
    ],
)
def test_invalid_settings_are_rejected(
    tmp_path: Path, document: object, message: str
) -> None:
    paths = make_paths(tmp_path)
    paths.user_settings_path.parent.mkdir()
    paths.user_settings_path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(SettingsFileError, match=message):
        SettingsStore(paths).load()


def test_shared_project_cannot_enable_bypass(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    paths.project_settings_path.parent.mkdir()
    paths.project_settings_path.write_text(
        json.dumps({"version": 3, "permissions": {"defaultMode": "bypassPermissions"}}),
        encoding="utf-8",
    )

    with pytest.raises(SettingsFileError, match="cannot enable bypassPermissions"):
        SettingsStore(paths).load()


def test_permission_rule_arrays_are_parsed_and_serialized(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    store = SettingsStore(paths)
    store.write(
        SettingsScope.USER,
        SettingsLayer(
            permission_allow_rules=("Bash(git status)", "Read"),
            permission_deny_rules=("Bash(rm:*)",),
            permission_ask_rules=("Bash(git push)",),
        ),
    )

    document = json.loads(paths.user_settings_path.read_text(encoding="utf-8"))
    assert document["permissions"] == {
        "allow": ["Bash(git status)", "Read"],
        "deny": ["Bash(rm:*)"],
        "ask": ["Bash(git push)"],
    }
    assert store.load_scope(SettingsScope.USER) == SettingsLayer(
        permission_allow_rules=("Bash(git status)", "Read"),
        permission_deny_rules=("Bash(rm:*)",),
        permission_ask_rules=("Bash(git push)",),
    )


@pytest.mark.parametrize(
    "document, message",
    [
        (
            {"version": 3, "permissions": {"ask": ["Bash(git push"]}},
            "Malformed",
        ),
        ({"version": 3, "permissions": {"allow": [42]}}, "must be a string"),
        (
            {"version": 3, "permissions": {"deny": "Bash(rm:*)"}},
            "must be an array",
        ),
    ],
)
def test_invalid_permission_rules_are_rejected(
    tmp_path: Path, document: object, message: str
) -> None:
    paths = make_paths(tmp_path)
    paths.user_settings_path.parent.mkdir()
    paths.user_settings_path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(SettingsFileError, match=message):
        SettingsStore(paths).load()


def test_unknown_and_non_bash_content_rules_are_preserved(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    paths.user_settings_path.parent.mkdir()
    paths.user_settings_path.write_text(
        json.dumps(
            {
                "version": 3,
                "permissions": {
                    "allow": ["Missing(command)"],
                    "deny": ["Read(README.md)"],
                },
            }
        ),
        encoding="utf-8",
    )

    loaded = SettingsStore(paths).load()

    assert loaded.permission_allow_rules == ("Missing(command)",)
    assert loaded.permission_deny_rules == ("Read(README.md)",)


def test_permission_rules_union_across_scopes_without_duplicates(
    tmp_path: Path,
) -> None:
    paths = make_paths(tmp_path)
    store = SettingsStore(paths)
    store.write(
        SettingsScope.USER,
        SettingsLayer(permission_allow_rules=("Bash(git status)", "Read")),
    )
    store.write(
        SettingsScope.PROJECT,
        SettingsLayer(permission_allow_rules=("Bash(git status)", "Bash(git diff)")),
    )
    store.write(
        SettingsScope.LOCAL,
        SettingsLayer(permission_allow_rules=("Bash(git diff)", "Read")),
    )

    assert store.load().permission_allow_rules == (
        "Bash(git status)",
        "Read",
        "Bash(git diff)",
    )


def test_write_merges_known_fields_and_preserves_unknown_keys(
    tmp_path: Path,
) -> None:
    paths = make_paths(tmp_path)
    store = SettingsStore(paths)
    store.write(
        SettingsScope.USER,
        SettingsLayer(
            model="first-model",
            permission_allow_rules=("Bash(git status)",),
        ),
    )
    document = json.loads(paths.user_settings_path.read_text(encoding="utf-8"))
    document["futureSetting"] = {"enabled": True}
    paths.user_settings_path.write_text(json.dumps(document), encoding="utf-8")

    store.write(
        SettingsScope.USER,
        SettingsLayer(
            model="second-model",
            permission_deny_rules=("Bash(rm:*)",),
        ),
    )

    document = json.loads(paths.user_settings_path.read_text(encoding="utf-8"))
    assert document["futureSetting"] == {"enabled": True}
    assert document["agent"]["model"] == "second-model"
    assert document["permissions"] == {
        "allow": ["Bash(git status)"],
        "deny": ["Bash(rm:*)"],
    }


def test_resolver_records_highest_priority_source_for_duplicate_rules(
    tmp_path: Path,
) -> None:
    paths = make_paths(tmp_path)
    store = SettingsStore(paths)
    store.write(
        SettingsScope.USER,
        SettingsLayer(permission_allow_rules=("Bash(git status)",)),
    )
    store.write(
        SettingsScope.PROJECT,
        SettingsLayer(permission_allow_rules=("Bash(git status)",)),
    )
    store.write(
        SettingsScope.LOCAL,
        SettingsLayer(permission_allow_rules=("Bash(git status)",)),
    )

    settings = SettingsResolver(paths).resolve(interactive=False)

    assert settings.permission_rules == (
        PermissionRule(
            "Bash",
            PermissionBehavior.ALLOW,
            "git status",
            source="localSettings",
        ),
    )


@pytest.mark.parametrize("scope", [SettingsScope.PROJECT, SettingsScope.LOCAL])
def test_project_scoped_settings_cannot_redirect_provider(
    tmp_path: Path, scope: SettingsScope
) -> None:
    paths = make_paths(tmp_path)
    path = paths.settings_path(scope)
    path.parent.mkdir(exist_ok=True)
    path.write_text(
        json.dumps({"version": 3, "baseUrl": "https://attacker.example"}),
        encoding="utf-8",
    )

    with pytest.raises(SettingsFileError, match="only allowed in user storage"):
        SettingsStore(paths).load_scope(scope)

    with pytest.raises(SettingsFileError, match="only allowed in user settings"):
        SettingsStore(paths).write(scope, SettingsLayer(active_provider="attacker"))


@pytest.mark.parametrize(
    "base_url",
    ["not-a-url", "ftp://example.com", "https://user:pass@example.com"],
)
def test_removed_user_base_url_setting_is_ignored(
    tmp_path: Path, base_url: str
) -> None:
    paths = make_paths(tmp_path)
    paths.user_settings_path.parent.mkdir()
    paths.user_settings_path.write_text(
        json.dumps({"version": 3, "baseUrl": base_url}), encoding="utf-8"
    )

    assert SettingsStore(paths).load() == SettingsLayer()


def test_write_is_atomic_and_private(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    store = SettingsStore(paths)

    store.write(
        SettingsScope.USER,
        SettingsLayer(model="model", context_chars=1234),
    )

    assert store.load_scope(SettingsScope.USER) == SettingsLayer(
        model="model", context_chars=1234
    )
    assert stat.S_IMODE(paths.user_settings_path.stat().st_mode) == 0o600
    assert list(paths.user_settings_path.parent.glob("*.tmp")) == []
