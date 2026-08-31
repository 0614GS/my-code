from pathlib import Path
from typing import Any

import pytest

from my_code.bootstrap import initialize_user_storage
from my_code.config.paths import MyCodePaths, SettingsScope
from my_code.config.providers import ProviderProtocol
from my_code.config.store import SettingsStore
from my_code.model.capabilities import ModelDescriptor
from my_code.providers.discovery import (
    ProviderProbeError,
    ProviderProbeResult,
)
from my_code.providers.manager import ProviderManager
from my_code.tui.provider_setup import ProviderSetupTui


def _manager(tmp_path: Path) -> tuple[ProviderManager, MyCodePaths]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    paths = MyCodePaths.discover(workspace, environ={}, home=tmp_path / "home")
    initialize_user_storage(paths)
    return ProviderManager(paths), paths


def _prompt(values: list[str]):
    async def prompt(_message: str, **_kwargs: Any) -> str:
        return values.pop(0)

    return prompt


@pytest.mark.asyncio
async def test_first_run_cancel_does_not_persist_partial_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, paths = _manager(tmp_path)

    async def fail_probe(*_args: Any, **_kwargs: Any) -> ProviderProbeResult:
        return ProviderProbeResult(
            False,
            (),
            "2026-08-28T00:00:00+00:00",
            ProviderProbeError.CONNECTION,
            "Could not connect.",
        )

    monkeypatch.setattr(manager, "probe", fail_probe)
    configured = await ProviderSetupTui(
        manager,
        prompt=_prompt(["1", "gateway", "", "temporary-key", "cancel"]),
    ).run()

    assert configured is False
    assert manager.profiles.load() == {}
    assert manager.credentials.load_api_key("gateway") is None
    assert SettingsStore(paths).load_scope(SettingsScope.USER).active_provider is None
    assert not paths.model_cache_path.exists()


@pytest.mark.asyncio
async def test_first_run_saves_only_after_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, paths = _manager(tmp_path)

    async def successful_probe(request: Any) -> ProviderProbeResult:
        assert manager.profiles.load() == {}
        assert manager.credentials.load_api_key("gateway") is None
        return ProviderProbeResult(
            True,
            (ModelDescriptor("model-a", "Model A"),),
            "2026-08-28T00:00:00+00:00",
            provider_id=request.provider_id,
            protocol=request.protocol,
            base_url=request.base_url,
        )

    monkeypatch.setattr(manager, "probe", successful_probe)
    configured = await ProviderSetupTui(
        manager,
        prompt=_prompt(
            ["2", "gateway", "https://gateway.example/v1", "secret", "model-a", "y"]
        ),
    ).run()

    assert configured is True
    profile = manager.profiles.load()["gateway"]
    assert profile.protocol is ProviderProtocol.OPENAI_RESPONSES
    assert profile.model == "model-a"
    assert manager.credentials.load_api_key("gateway") == "secret"
    assert (
        SettingsStore(paths).load_scope(SettingsScope.USER).active_provider == "gateway"
    )
