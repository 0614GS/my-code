"""Configuration path ownership tests."""

from pathlib import Path

from nano_code.config import NanoCodePaths, SettingsScope, sanitize_path

_SESSION_ID = "12345678-1234-1234-1234-123456789abc"


def test_default_layout_matches_claude_code_shape(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    home = tmp_path / "home"

    paths = NanoCodePaths.discover(workspace, environ={}, home=home)

    assert paths.config_home == home / ".nano-code"
    assert paths.project_state_dir == (
        home / ".nano-code" / "projects" / sanitize_path(str(workspace.resolve()))
    )
    assert paths.tool_results_dir(_SESSION_ID) == (
        paths.project_state_dir / _SESSION_ID / "tool-results"
    )
    assert paths.settings_path(SettingsScope.USER) == (
        home / ".nano-code" / "settings.json"
    )
    assert paths.credentials_path == home / ".nano-code" / ".credentials.json"
    assert paths.providers_path == home / ".nano-code" / "providers.json"
    assert paths.settings_path(SettingsScope.PROJECT) == (
        workspace / ".nano-code" / "settings.json"
    )
    assert paths.settings_path(SettingsScope.LOCAL) == (
        workspace / ".nano-code" / "settings.local.json"
    )


def test_config_home_can_be_overridden(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    override = tmp_path / "private-state"

    paths = NanoCodePaths.discover(
        workspace,
        environ={"NANO_CODE_CONFIG_DIR": str(override)},
    )

    assert paths.config_home == override


def test_sanitize_path_is_portable_and_bounds_long_names() -> None:
    assert sanitize_path("/home/user/my-project") == "-home-user-my-project"
    first = sanitize_path("/" + "a" * 240)
    second = sanitize_path("/" + "a" * 239 + "b")

    assert len(first) == 217
    assert first[:200] == "-" + "a" * 199
    assert first != second
