import json
import stat
from pathlib import Path

import pytest

from my_code.features.background_tasks.bash import BashBackgroundController
from my_code.features.background_tasks.registry import BackgroundTaskRegistry
from my_code.features.background_tasks.wake import BackgroundTaskWakeSignal
from my_code.tasks.models import TaskStatus
from my_code.tasks.supervisor import TaskSupervisor
from my_code.tools.base import ToolExecutionContext
from my_code.tools.builtin.bash import BashTool


def build_tool(
    tmp_path: Path,
) -> tuple[BashTool, TaskSupervisor, BackgroundTaskRegistry]:
    tasks = TaskSupervisor()
    registry = BackgroundTaskRegistry(tasks, BackgroundTaskWakeSignal())
    controller = BashBackgroundController(
        tasks,
        registry,
        lambda session_id: tmp_path / "runtime" / session_id / "tasks",
    )
    tool = BashTool(
        background_executor=controller,
        background_enabled=True,
    )
    return tool, tasks, registry


def test_background_schema_explains_explicit_and_default_execution_modes(
    tmp_path: Path,
) -> None:
    tool, _, _ = build_tool(tmp_path)

    properties = tool.definition.input_schema["properties"]
    assert isinstance(properties, dict)
    background = properties["background"]
    assert isinstance(background, dict)
    assert background["default"] is False
    assert background["description"] == (
        "When true, run asynchronously and return a task ID immediately. "
        "When false or omitted, wait for completion up to the timeout; if that "
        "wait expires, the command continues in the background."
    )


@pytest.mark.asyncio
async def test_supervised_foreground_uses_and_removes_private_output(
    tmp_path: Path,
) -> None:
    tool, tasks, _ = build_tool(tmp_path)

    output = await tool.execute(
        {"command": "printf 'out\\n'; printf 'err\\n' >&2"},
        ToolExecutionContext(tmp_path, session_id="owner"),
    )

    assert output.content == "exit_code: 0\nout\nerr\n"
    assert list((tmp_path / "runtime").rglob("*.output")) == []

    failed = await tool.execute(
        {"command": "printf failure; exit 9"},
        ToolExecutionContext(tmp_path, session_id="owner"),
    )
    assert failed.is_error is True
    assert failed.metadata["exit_code"] == 9
    assert list((tmp_path / "runtime").rglob("*.output")) == []
    await tasks.close()


@pytest.mark.asyncio
async def test_explicit_background_retains_output_and_pulses_once(
    tmp_path: Path,
) -> None:
    tool, tasks, registry = build_tool(tmp_path)
    signal = registry.wake_signal
    assert signal is not None

    output = await tool.execute(
        {"command": "printf background", "background": True},
        ToolExecutionContext(tmp_path, run_id="owner", session_id="owner"),
    )
    payload = json.loads(output.content)
    output_file = Path(payload["output_file"])
    assert payload["background_reason"] == "requested"
    assert stat.S_IMODE(output_file.stat().st_mode) == 0o600  # noqa: ASYNC240

    snapshot = await tasks.wait(payload["task_id"])

    assert snapshot.status is TaskStatus.SUCCEEDED
    assert output_file.read_text(encoding="utf-8") == "background"  # noqa: ASYNC240
    assert signal.revision == 1
    registry.terminal(snapshot)
    assert signal.revision == 1
    completion = registry.payload(registry.pending("owner")[0])
    assert completion["task_type"] == "bash"
    assert completion["exit_code"] == 0
    assert "result" not in completion
    await tasks.close()


@pytest.mark.asyncio
async def test_background_output_uses_execution_session_directory(
    tmp_path: Path,
) -> None:
    tool, tasks, _ = build_tool(tmp_path)
    target_session = "22222222-2222-2222-2222-222222222222"

    output = await tool.execute(
        {"command": "printf resumed", "background": True},
        ToolExecutionContext(
            tmp_path,
            run_id="live-run",
            session_id=target_session,
            root_session_id=target_session,
        ),
    )
    payload = json.loads(output.content)

    assert Path(payload["output_file"]).parent == (
        tmp_path / "runtime" / target_session / "tasks"
    )
    await tasks.close()


@pytest.mark.asyncio
async def test_foreground_budget_hands_same_process_to_background(
    tmp_path: Path,
) -> None:
    tool, tasks, registry = build_tool(tmp_path)
    context = ToolExecutionContext(tmp_path, run_id="owner", session_id="owner")

    assert tool.background_executor is not None
    output = await tool.background_executor.execute(
        "printf before; sleep .1; printf after",
        context,
        0.01,
        background=False,
    )
    payload = json.loads(output.content)
    assert payload["background_reason"] == "timeout"

    await tasks.wait(payload["task_id"])
    assert (
        Path(payload["output_file"]).read_text(encoding="utf-8")  # noqa: ASYNC240
        == "beforeafter"
    )
    assert registry.payload(registry.pending("owner")[0])["status"] == "succeeded"
    await tasks.close()


@pytest.mark.asyncio
async def test_background_nonzero_is_failed_and_cancel_is_cancelled(
    tmp_path: Path,
) -> None:
    tool, tasks, registry = build_tool(tmp_path)
    context = ToolExecutionContext(tmp_path, run_id="owner", session_id="owner")
    failed = json.loads(
        (
            await tool.execute(
                {"command": "printf bad; exit 7", "background": True}, context
            )
        ).content
    )
    await tasks.wait(failed["task_id"])
    failed_payload = registry.payload(registry.get("owner", failed["task_id"]))
    assert failed_payload["status"] == "failed"
    assert failed_payload["exit_code"] == 7

    running = json.loads(
        (
            await tool.execute({"command": "sleep 10", "background": True}, context)
        ).content
    )
    await registry.cancel("owner", running["task_id"])
    assert tasks.snapshot(running["task_id"]).status is TaskStatus.CANCELLED
    await tasks.close()


@pytest.mark.asyncio
async def test_output_cap_and_runtime_shutdown_are_terminal_failures(
    tmp_path: Path,
) -> None:
    tool, tasks, registry = build_tool(tmp_path)
    context = ToolExecutionContext(
        tmp_path,
        max_command_output_bytes=8,
        run_id="owner",
        session_id="owner",
    )
    capped = json.loads(
        (
            await tool.execute(
                {"command": "printf 123456789", "background": True}, context
            )
        ).content
    )
    await tasks.wait(capped["task_id"])
    assert tasks.snapshot(capped["task_id"]).status is TaskStatus.FAILED

    running = json.loads(
        (
            await tool.execute({"command": "sleep 10", "background": True}, context)
        ).content
    )
    await tasks.close()
    payload = registry.payload(registry.get("owner", running["task_id"]))
    assert payload["status"] == "cancelled"
    assert payload["error_kind"] == "shutdown"
