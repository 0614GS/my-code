from pathlib import Path

import pytest

from nano_code.auth import CredentialStore
from nano_code.cli.arguments import AuthAction, AuthOptions
from nano_code.cli.auth import run_auth_command
from nano_code.config import NanoCodePaths


def _options(tmp_path: Path, action: AuthAction) -> AuthOptions:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    paths = NanoCodePaths.discover(
        workspace,
        environ={"NANO_CODE_CONFIG_DIR": str(tmp_path / "config")},
    )
    return AuthOptions(action=action, paths=paths, provider_id="anthropic")


def test_login_status_and_logout_lifecycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("NANO_CODE_API_KEY", raising=False)
    login = _options(tmp_path, AuthAction.LOGIN)

    assert run_auth_command(login, secret_input=lambda _prompt: "secret-key") == 0
    assert CredentialStore(login.paths.credentials_path).load_api_key() == "secret-key"
    assert "secret-key" not in capsys.readouterr().out

    assert run_auth_command(_options(tmp_path, AuthAction.STATUS)) == 0
    status_output = capsys.readouterr().out
    assert "authenticated via stored" in status_output
    assert "secret-key" not in status_output

    assert run_auth_command(_options(tmp_path, AuthAction.LOGOUT)) == 0
    assert login.paths.credentials_path.exists()
    assert CredentialStore(login.paths.credentials_path).load_api_key() is None
