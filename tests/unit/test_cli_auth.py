from pathlib import Path

import pytest

from my_code.auth.credentials import CredentialStore
from my_code.cli.arguments import AuthAction, AuthOptions
from my_code.cli.auth import run_auth_command
from my_code.config.paths import MyCodePaths


def _options(tmp_path: Path, action: AuthAction) -> tuple[AuthOptions, MyCodePaths]:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    paths = MyCodePaths.discover(
        workspace,
        environ={"MY_CODE_CONFIG_DIR": str(tmp_path / "config")},
    )
    return (
        AuthOptions(action=action, cwd=workspace, provider_override=None),
        paths,
    )


def test_login_status_and_logout_lifecycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("MY_CODE_API_KEY", raising=False)
    login, paths = _options(tmp_path, AuthAction.LOGIN)

    assert (
        run_auth_command(
            login,
            paths,
            "anthropic",
            secret_input=lambda _prompt: "secret-key",
        )
        == 0
    )
    assert CredentialStore(paths.credentials_path).load_api_key() == "secret-key"
    assert "secret-key" not in capsys.readouterr().out

    status, _ = _options(tmp_path, AuthAction.STATUS)
    assert run_auth_command(status, paths, "anthropic") == 0
    status_output = capsys.readouterr().out
    assert "authenticated via stored" in status_output
    assert "secret-key" not in status_output

    logout, _ = _options(tmp_path, AuthAction.LOGOUT)
    assert run_auth_command(logout, paths, "anthropic") == 0
    assert paths.credentials_path.exists()
    assert CredentialStore(paths.credentials_path).load_api_key() is None
