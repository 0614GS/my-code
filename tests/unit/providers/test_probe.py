import asyncio
from pathlib import Path

import pytest

from my_code.bootstrap import initialize_user_storage
from my_code.config.paths import MyCodePaths
from my_code.config.providers import ProviderProtocol
from my_code.model.capabilities import ModelDescriptor
from my_code.providers import discovery
from my_code.providers.discovery import ProviderProbeError, ProviderProbeRequest
from my_code.providers.manager import ProviderManager


def _manager(tmp_path: Path) -> tuple[ProviderManager, MyCodePaths]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    paths = MyCodePaths.discover(workspace, environ={}, home=tmp_path / "home")
    initialize_user_storage(paths)
    return ProviderManager(paths), paths


@pytest.mark.asyncio
async def test_unsaved_probe_is_online_only_and_does_not_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, paths = _manager(tmp_path)
    before_profiles = paths.providers_path.read_bytes()
    before_credentials = paths.credentials_path.read_bytes()
    closed = False

    class Catalog:
        async def list_models(self) -> tuple[ModelDescriptor, ...]:
            return (ModelDescriptor("model-a", "Model A"),)

    async def close() -> None:
        nonlocal closed
        closed = True

    monkeypatch.setattr(discovery, "_catalog_for", lambda *_args: (Catalog(), close))

    result = await manager.probe(
        ProviderProbeRequest(
            "new-provider",
            ProviderProtocol.OPENAI_RESPONSES,
            "https://gateway.example/v1",
            "secret-key",
        )
    )

    assert result.succeeded is True
    assert [item.id for item in result.models] == ["model-a"]
    assert closed is True
    assert paths.providers_path.read_bytes() == before_profiles
    assert paths.credentials_path.read_bytes() == before_credentials
    assert not paths.model_cache_path.exists()
    assert "secret-key" not in repr(result)


@pytest.mark.asyncio
async def test_probe_timeout_closes_client_and_returns_safe_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, _paths = _manager(tmp_path)
    closed = False

    class Catalog:
        async def list_models(self) -> tuple[ModelDescriptor, ...]:
            import asyncio

            await asyncio.Event().wait()
            return ()

    async def close() -> None:
        nonlocal closed
        closed = True

    monkeypatch.setattr(discovery, "_catalog_for", lambda *_args: (Catalog(), close))

    result = await manager.probe(
        ProviderProbeRequest(
            "new-provider",
            ProviderProtocol.ANTHROPIC_MESSAGES,
            None,
            "do-not-leak",
        ),
        timeout_seconds=0.001,
    )

    assert result.succeeded is False
    assert result.error_kind is ProviderProbeError.TIMEOUT
    assert closed is True
    assert "do-not-leak" not in repr(result)
    assert "do-not-leak" not in (result.error_message or "")


@pytest.mark.asyncio
async def test_probe_classifies_authentication_without_response_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, _paths = _manager(tmp_path)

    class AuthenticationFailure(Exception):
        status_code = 401

    class Catalog:
        async def list_models(self) -> tuple[ModelDescriptor, ...]:
            raise AuthenticationFailure("secret-key and raw response")

    async def close() -> None:
        return None

    monkeypatch.setattr(discovery, "_catalog_for", lambda *_args: (Catalog(), close))
    result = await manager.probe(
        ProviderProbeRequest(
            "new-provider",
            ProviderProtocol.ANTHROPIC_MESSAGES,
            None,
            "secret-key",
        )
    )

    assert result.error_kind is ProviderProbeError.AUTHENTICATION
    assert result.models == ()
    assert "secret-key" not in (result.error_message or "")
    assert "raw response" not in (result.error_message or "")


@pytest.mark.asyncio
async def test_probe_cancellation_propagates_and_closes_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, _paths = _manager(tmp_path)
    started = asyncio.Event()
    closed = False

    class Catalog:
        async def list_models(self) -> tuple[ModelDescriptor, ...]:
            started.set()
            await asyncio.Event().wait()
            return ()

    async def close() -> None:
        nonlocal closed
        closed = True

    monkeypatch.setattr(discovery, "_catalog_for", lambda *_args: (Catalog(), close))
    task = asyncio.create_task(
        manager.probe(
            ProviderProbeRequest(
                "new-provider",
                ProviderProtocol.ANTHROPIC_MESSAGES,
                None,
            )
        )
    )
    await started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed is True
