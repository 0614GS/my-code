import asyncio
from pathlib import Path

import pytest

from nano_code.tools.base import ToolContext, ToolExecutionError
from nano_code.tools.builtin.bash.process import execute_bash, subprocess_environment


class _FakeProcess:
    def __init__(self, output: bytes = b"ok") -> None:
        self.stdout = asyncio.StreamReader()
        self.stdout.feed_data(output)
        self.stdout.feed_eof()
        self.returncode: int | None = None
        self.pid = 12345

    async def wait(self) -> int:
        self.returncode = 0
        return 0


@pytest.mark.asyncio
async def test_executor_uses_exact_bash_argv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: list[tuple[tuple[object, ...], dict[str, object]]] = []

    async def fake_create_subprocess_exec(
        *args: object, **kwargs: object
    ) -> _FakeProcess:
        captured.append((args, kwargs))
        return _FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    output = await execute_bash("printf ok", ToolContext(tmp_path), 5)

    assert output.is_error is False
    assert captured[0][0] == ("/bin/bash", "-c", "printf ok")
    assert captured[0][1]["cwd"] == tmp_path
    assert captured[0][1]["start_new_session"] is True


@pytest.mark.asyncio
async def test_missing_bash_executable_is_explicit_tool_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def missing(*args: object, **kwargs: object) -> _FakeProcess:
        del args, kwargs
        raise FileNotFoundError

    monkeypatch.setattr(asyncio, "create_subprocess_exec", missing)

    with pytest.raises(ToolExecutionError, match="/bin/bash.*unavailable"):
        await execute_bash("pwd", ToolContext(tmp_path), 5)


def test_subprocess_environment_removes_secrets_and_bash_injection_variables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    removed = [
        "NANO_CODE_API_KEY",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "BASH_ENV",
        "ENV",
        "SHELLOPTS",
        "BASHOPTS",
        "CDPATH",
        "BASH_FUNC_attack%%",
    ]
    for name in removed:
        monkeypatch.setenv(name, "unsafe")
    monkeypatch.setenv("NANO_CODE_TEST_SAFE", "kept")

    environment = subprocess_environment()

    assert all(name not in environment for name in removed)
    assert environment["NANO_CODE_TEST_SAFE"] == "kept"


@pytest.mark.asyncio
async def test_nonzero_exit_is_a_structured_error(tmp_path: Path) -> None:
    output = await execute_bash("printf failure; exit 7", ToolContext(tmp_path), 5)

    assert output.is_error is True
    assert output.metadata["exit_code"] == 7
    assert "failure" in output.content


@pytest.mark.asyncio
async def test_timeout_terminates_bash_process_group(tmp_path: Path) -> None:
    with pytest.raises(ToolExecutionError, match="timed out"):
        await execute_bash("sleep 10", ToolContext(tmp_path), 1)


@pytest.mark.asyncio
async def test_cancellation_terminates_bash_process_group(tmp_path: Path) -> None:
    task = asyncio.create_task(execute_bash("sleep 10", ToolContext(tmp_path), 30))
    await asyncio.sleep(0.05)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_output_limit_terminates_command(tmp_path: Path) -> None:
    context = ToolContext(tmp_path, max_command_output_bytes=8)

    with pytest.raises(ToolExecutionError, match="output exceeded"):
        await execute_bash("printf 123456789", context, 5)
