from pathlib import Path

import pytest

from nano_code.agent import AgentMaxStepsReached, AgentTurnInput
from nano_code.cli import main as cli_main
from nano_code.cli.arguments import parse_args
from nano_code.conversation import TokenUsage
from nano_code.core import SettingsResolver


class LimitedAgent:
    async def submit(self, turn_input: AgentTurnInput) -> AgentMaxStepsReached:
        assert turn_input.prompt == "keep going"
        return AgentMaxStepsReached(3, 3, TokenUsage(12, 4))


@pytest.mark.asyncio
async def test_print_mode_maps_max_steps_outcome_to_error_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config_home = tmp_path / "config"
    monkeypatch.setenv("NANO_CODE_CONFIG_DIR", str(config_home))
    options = parse_args(
        ["--cwd", str(workspace), "--max-steps", "3", "-p", "keep going"]
    )
    resolver = SettingsResolver.for_workspace(options.cwd)
    monkeypatch.setattr(
        cli_main,
        "bootstrap_agent",
        lambda _settings, _session_id: LimitedAgent(),
    )

    exit_code = await cli_main.run(options, resolver)

    assert exit_code == 1
    assert "Reached max steps (3)" in capsys.readouterr().err
    assert not config_home.exists()
