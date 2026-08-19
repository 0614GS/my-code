from pathlib import Path

import pytest

import my_code.bootstrap as cli_main
from my_code.chat.events import MaxStepsReached
from my_code.cli.arguments import parse_args
from my_code.config.settings import SettingsResolver


class LimitedChat:
    async def submit(self, prompt: str) -> MaxStepsReached:
        assert prompt == "keep going"
        return MaxStepsReached(3, 3, 12, 4)


@pytest.mark.asyncio
async def test_print_mode_maps_max_steps_outcome_to_error_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config_home = tmp_path / "config"
    monkeypatch.setenv("MY_CODE_CONFIG_DIR", str(config_home))
    options = parse_args(
        ["--cwd", str(workspace), "--max-steps", "3", "-p", "keep going"]
    )
    resolver = SettingsResolver.for_workspace(options.cwd)
    monkeypatch.setattr(
        cli_main,
        "bootstrap_chat",
        lambda _settings, _session_id: LimitedChat(),
    )

    exit_code = await cli_main.run(options, resolver)

    assert exit_code == 1
    assert "Reached max steps (3)" in capsys.readouterr().err
    assert not config_home.exists()
