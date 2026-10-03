"""后台等待、订阅和有界输出的回归测试。"""

import asyncio
import json
from pathlib import Path
from uuid import uuid4

import pytest

from my_code.conversation.attachments import BackgroundTaskCompletionAttachment
from my_code.features.background_tasks.notifications import (
    BackgroundTaskNotificationSource,
)
from my_code.features.background_tasks.registry import (
    BackgroundTask,
    BackgroundTaskRegistry,
)
from my_code.features.subagents.models import SubagentParentContext
from my_code.features.subagents.task_tools import (
    TaskListTool,
    TaskWaitTool,
    TaskWatchTool,
)
from my_code.foundation.json import JsonObject
from my_code.tasks.models import TaskStatus
from my_code.tasks.supervisor import TaskSupervisor
from my_code.tools.base import ToolExecutionContext, ToolInputError


async def _start_task(
    registry: BackgroundTaskRegistry,
    release: asyncio.Event,
    *,
    output_file: Path | None = None,
    fail: bool = False,
) -> str:
    task_id = str(uuid4())
    details: JsonObject = (
        {"output_file": str(output_file)} if output_file is not None else {}
    )
    registry.register(
        BackgroundTask(
            task_id, "owner", "bash" if output_file else "subagent", "work", details
        )
    )

    async def run() -> None:
        await release.wait()
        if fail:
            raise RuntimeError("failed")

    await registry.tasks.submit(
        run, name="work", task_id=task_id, on_terminal=registry.terminal
    )
    return task_id


def _tools(registry: BackgroundTaskRegistry) -> tuple[TaskWaitTool, TaskWatchTool]:
    parent = SubagentParentContext("owner")
    return TaskWaitTool(registry, parent=parent), TaskWatchTool(registry, parent=parent)


@pytest.mark.asyncio
async def test_wait_times_out_then_completes_without_polling(tmp_path: Path) -> None:
    supervisor = TaskSupervisor()
    registry = BackgroundTaskRegistry(supervisor)
    release = asyncio.Event()
    output_file = tmp_path / "log"
    output_file.write_bytes(b"before")
    task_id = await _start_task(registry, release, output_file=output_file)
    wait_tool, _ = _tools(registry)
    context = ToolExecutionContext(tmp_path, session_id="owner")

    assert await registry.wait("owner", task_id, 0.01) == "timeout"
    first = registry.output_since(registry.get("owner", task_id), 0)
    assert first["output"] == "before"
    assert first["next_output_offset"] == 6

    output_file.write_bytes(b"beforeafter")
    release.set()
    result = await wait_tool.execute({"task_id": task_id, "output_offset": 6}, context)
    payload = json.loads(result.content)
    assert payload["end_reason"] == "completed"
    assert payload["output"] == "after"
    assert payload["next_output_offset"] == 11
    assert len(result.new_attachments) == 1
    attachment = result.new_attachments[0]
    assert isinstance(attachment, BackgroundTaskCompletionAttachment)
    assert attachment.result["output_preview"] == "beforeafter"
    assert attachment.result["output_total_bytes"] == 11
    source = BackgroundTaskNotificationSource(registry)
    source.acknowledge(result.new_attachments)
    assert registry.pending("owner") == ()
    await supervisor.close()


@pytest.mark.asyncio
async def test_user_input_interrupts_wait_but_task_continues(tmp_path: Path) -> None:
    supervisor = TaskSupervisor()
    registry = BackgroundTaskRegistry(supervisor)
    release = asyncio.Event()
    task_id = await _start_task(registry, release)
    wait_tool, _ = _tools(registry)
    waiting = asyncio.create_task(
        wait_tool.execute(
            {"task_id": task_id}, ToolExecutionContext(tmp_path, session_id="owner")
        )
    )
    await asyncio.sleep(0)
    registry.interrupt_waiters("owner")
    result = await asyncio.wait_for(waiting, 1)
    assert json.loads(result.content)["end_reason"] == "user_input"
    assert result.new_attachments == ()
    assert not supervisor.snapshot(task_id).status.terminal
    release.set()
    await supervisor.wait(task_id)
    await supervisor.close()


@pytest.mark.asyncio
async def test_watch_only_active_tasks_and_cancel_clears_subscription(
    tmp_path: Path,
) -> None:
    supervisor = TaskSupervisor()
    registry = BackgroundTaskRegistry(supervisor)
    wait_tool, watch_tool = _tools(registry)
    release = asyncio.Event()
    task_id = await _start_task(registry, release, fail=True)
    context = ToolExecutionContext(tmp_path, session_id="owner")
    watched = await watch_tool.execute({"task_id": task_id}, context)
    assert json.loads(watched.content)["watching"] is True
    release.set()
    await supervisor.wait(task_id)
    assert registry.watched_pending("owner")
    failed = await wait_tool.execute({"task_id": task_id}, context)
    assert json.loads(failed.content)["status"] == "failed"
    BackgroundTaskNotificationSource(registry).acknowledge(failed.new_attachments)
    assert registry.watched_pending("owner") == ()
    assert (
        json.loads((await watch_tool.execute({"task_id": task_id}, context)).content)[
            "watching"
        ]
        is False
    )

    release2 = asyncio.Event()
    second = await _start_task(registry, release2)
    await watch_tool.execute({"task_id": second}, context)
    await registry.cancel("owner", second)
    assert registry.watched_pending("owner") == ()
    assert supervisor.snapshot(second).status is TaskStatus.CANCELLED
    cancelled = await wait_tool.execute({"task_id": second}, context)
    assert json.loads(cancelled.content)["status"] == "cancelled"
    assert len(cancelled.new_attachments) == 1
    await supervisor.close()


@pytest.mark.asyncio
async def test_bounded_output_and_validation(tmp_path: Path) -> None:
    supervisor = TaskSupervisor()
    registry = BackgroundTaskRegistry(supervisor)
    release = asyncio.Event()
    output_file = tmp_path / "log"
    output_file.write_bytes(b"A" * 5000 + b"B" * 5000)
    task_id = await _start_task(registry, release, output_file=output_file)
    item = registry.get("owner", task_id)
    listed = await TaskListTool(
        registry, parent=SubagentParentContext("owner")
    ).execute({}, ToolExecutionContext(tmp_path, session_id="owner"))
    listed_preview = json.loads(listed.content)["tasks"][0]["output_preview"]
    assert len(listed_preview.encode()) <= 1024
    running = registry.payload(item)
    assert len(str(running["output_preview"]).encode()) <= 1024
    assert running["output_truncated"] is True
    chunk = registry.output_since(item, 0)
    assert chunk["omitted_bytes"] == 1808
    output = chunk["output"]
    assert isinstance(output, str)
    assert output.startswith("A")
    assert output.endswith("B")
    assert chunk["next_output_offset"] == 10000
    release.set()
    await supervisor.wait(task_id)
    completed = registry.payload(item)
    assert len(str(completed["output_preview"]).encode()) <= 4096
    output_file.write_bytes(b"\xff" * 10000)
    invalid = registry.output_since(item, 0)
    invalid_output = invalid["output"]
    assert isinstance(invalid_output, str)
    assert len(invalid_output.encode("utf-8")) <= 8192
    assert invalid["output_display_truncated"] is True
    output_file.write_text("\n".join(str(n) for n in range(10)), encoding="utf-8")
    assert registry.output_preview(item, terminal=False)["output_preview"] == (
        "5\n6\n7\n8\n9"
    )
    output_file.write_bytes(b"")
    assert registry.output_since(item, 0)["output"] == ""
    assert registry.output_preview(item, terminal=True)["output_truncated"] is False
    wait_tool, _ = _tools(registry)
    for invalid in (
        {"task_id": task_id, "timeout_seconds": 4},
        {"task_id": task_id, "output_offset": -1},
    ):
        with pytest.raises(ToolInputError):
            wait_tool.validate_input(invalid)
    await supervisor.close()
