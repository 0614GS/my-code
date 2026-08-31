"""SAFE-01 ordered shutdown reaps tasks before run/provider resources."""

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

from my_code.auth.credentials import CredentialSource
from my_code.bootstrap import bootstrap_chat
from my_code.config.paths import MyCodePaths
from my_code.config.settings import AgentSettings
from my_code.permissions.models import PermissionMode
from my_code.runtime.runs import AgentRunSpec
from my_code.runtime.state import ProviderRuntime
from my_code.sessions.session import Session
from my_code.tasks.models import TaskStatus


def settings(tmp_path: Path) -> AgentSettings:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return AgentSettings(
        paths=MyCodePaths.discover(
            workspace,
            environ={},
            home=tmp_path / "home",
        ),
        provider_id="anthropic",
        model="test-model",
        permission_mode=PermissionMode.DEFAULT,
        max_steps=3,
        max_output_tokens=100,
        interactive=False,
        credential_source=CredentialSource.NONE,
    )


@pytest.mark.asyncio
async def test_runtime_shutdown_cancels_tasks_before_closing_run_leases(
    tmp_path: Path,
) -> None:
    runtime = bootstrap_chat(
        settings(tmp_path),
        "11111111-1111-1111-1111-111111111111",
    )
    child_session = Session(
        runtime.settings.paths.project_state_dir,
        "22222222-2222-2222-2222-222222222222",
    )
    run = await runtime.state.runs.create(AgentRunSpec(child_session, "child"))
    started = asyncio.Event()
    cancellation_observation: list[tuple[bool, bool]] = []

    async def blocked() -> object:
        started.set()
        try:
            await asyncio.Future[None]()
        finally:
            cancellation_observation.append((run.closed, run.provider.closed))

    handle = await runtime.state.tasks.submit(blocked, name="child-task")
    await started.wait()

    await runtime.close()

    assert cancellation_observation == [(False, False)]
    task_snapshot = handle.snapshot()
    assert task_snapshot.status is TaskStatus.CANCELLED
    assert task_snapshot.failure is not None
    assert task_snapshot.failure.kind == "shutdown"
    assert run.closed is True
    assert runtime.state.runs.active_count == 0
    assert runtime.state.provider.leases.active_count == 0
    assert not any(
        not task.done() and task.get_name().startswith("my-code:")
        for task in asyncio.all_tasks()
        if task is not asyncio.current_task()
    )

    await runtime.close()


@pytest.mark.asyncio
async def test_runtime_shutdown_order_is_tasks_runs_skills_mcp_then_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = bootstrap_chat(settings(tmp_path))
    order: list[str] = []

    def observing(
        name: str, close: Callable[[], Awaitable[None]]
    ) -> Callable[[], Awaitable[None]]:
        async def wrapped() -> None:
            order.append(name)
            await close()

        return wrapped

    monkeypatch.setattr(
        runtime.state.tasks,
        "close",
        observing("tasks", runtime.state.tasks.close),
    )
    monkeypatch.setattr(
        runtime.state.runs,
        "close",
        observing("runs", runtime.state.runs.close),
    )
    monkeypatch.setattr(
        runtime.state.skills,
        "close",
        observing("skills", runtime.state.skills.close),
    )
    monkeypatch.setattr(
        runtime.state.mcp,
        "close",
        observing("mcp", runtime.state.mcp.close),
    )
    provider = runtime.state.provider
    original_provider_close = ProviderRuntime.close

    async def close_provider(candidate: ProviderRuntime) -> None:
        assert candidate is provider
        order.append("provider")
        await original_provider_close(candidate)

    monkeypatch.setattr(ProviderRuntime, "close", close_provider)

    await runtime.close()

    assert order == ["tasks", "runs", "skills", "mcp", "provider"]
