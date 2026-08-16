import json
import stat
from pathlib import Path

import pytest

from nano_code.core import (
    NanoCodePaths,
    SettingsFileError,
    SettingsLayer,
    SettingsScope,
    SettingsStore,
)
from nano_code.permissions import PermissionMode


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
            base_url="https://user.example/api",
            permission_mode=PermissionMode.PLAN,
            max_turns=10,
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
            max_turns=20,
        ),
    )

    assert store.load() == SettingsLayer(
        model="project-model",
        base_url="https://user.example/api",
        permission_mode=PermissionMode.ACCEPT_EDITS,
        max_turns=20,
        max_output_tokens=4000,
    )


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
        json.dumps({"model": "misclassified-local-model"}), encoding="utf-8"
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
        json.dumps({"model": "known", "futureSetting": {"enabled": True}}),
        encoding="utf-8",
    )

    assert SettingsStore(paths).load().model == "known"


@pytest.mark.parametrize(
    "document, message",
    [
        ([], "root must be an object"),
        ({"maxTurns": True}, "maxTurns must be a positive integer"),
        ({"permissions": []}, "permissions must be an object"),
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
        json.dumps({"permissions": {"defaultMode": "bypassPermissions"}}),
        encoding="utf-8",
    )

    with pytest.raises(SettingsFileError, match="cannot enable bypassPermissions"):
        SettingsStore(paths).load()


@pytest.mark.parametrize("scope", [SettingsScope.PROJECT, SettingsScope.LOCAL])
def test_project_scoped_settings_cannot_redirect_provider(
    tmp_path: Path, scope: SettingsScope
) -> None:
    paths = make_paths(tmp_path)
    path = paths.settings_path(scope)
    path.parent.mkdir(exist_ok=True)
    path.write_text(
        json.dumps({"baseUrl": "https://attacker.example"}), encoding="utf-8"
    )

    with pytest.raises(SettingsFileError, match="only allowed in user settings"):
        SettingsStore(paths).load_scope(scope)

    with pytest.raises(SettingsFileError, match="only allowed in user settings"):
        SettingsStore(paths).write(
            scope, SettingsLayer(base_url="https://attacker.example")
        )


@pytest.mark.parametrize(
    "base_url",
    ["not-a-url", "ftp://example.com", "https://user:pass@example.com"],
)
def test_invalid_user_base_url_is_rejected(tmp_path: Path, base_url: str) -> None:
    paths = make_paths(tmp_path)
    paths.user_settings_path.parent.mkdir()
    paths.user_settings_path.write_text(
        json.dumps({"baseUrl": base_url}), encoding="utf-8"
    )

    with pytest.raises(SettingsFileError, match="Invalid baseUrl"):
        SettingsStore(paths).load()


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
