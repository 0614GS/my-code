"""Supervised Bash lifecycle and foreground-to-background handoff."""

import asyncio
import json
from pathlib import Path
from uuid import uuid4

from my_code.features.background_tasks.registry import (
    BackgroundTask,
    BackgroundTaskRegistry,
    secure_task_output_path,
)
from my_code.foundation.json import JsonObject
from my_code.tasks.models import TaskStatus
from my_code.tasks.supervisor import TaskSupervisor
from my_code.tools.base import ToolContext, ToolOutput
from my_code.tools.builtin.bash.process import (
    BashTaskFailed,
    execute_bash_to_file,
)
from my_code.tools.presentation import compact_text


class BashBackgroundController:
    def __init__(
        self,
        tasks: TaskSupervisor,
        registry: BackgroundTaskRegistry,
        output_dir: Path,
        owner_run_id: str,
    ) -> None:
        self.tasks = tasks
        self.registry = registry
        self.output_dir = output_dir
        self.owner_run_id = owner_run_id

    async def execute(
        self,
        command: str,
        context: ToolContext,
        foreground_budget: float,
        *,
        background: bool,
        authority: str = "use_default",
        escalation_available: bool = False,
    ) -> ToolOutput:
        task_id = str(uuid4())
        output_file = secure_task_output_path(self.output_dir, task_id)
        owner = context.run_id or self.owner_run_id
        details: JsonObject = {
            "command": compact_text(command),
            "output_file": str(output_file),
            "execution_backend": context.command_launcher.status.display,
            "authority": authority,
            "escalation_available": escalation_available,
        }
        item = BackgroundTask(task_id, owner, "bash", compact_text(command), details)

        async def runner() -> object:
            try:
                outcome = await execute_bash_to_file(
                    command, context, output_file, authority=authority
                )
            except BashTaskFailed as error:
                if error.exit_code is not None:
                    details["exit_code"] = error.exit_code
                raise
            details["exit_code"] = outcome.exit_code
            return outcome

        if background:
            self.registry.register(item)
        try:
            handle = await self.tasks.submit(
                runner,
                name=f"bash:{compact_text(command)}",
                task_id=task_id,
                on_terminal=self.registry.terminal,
            )
        except BaseException:
            self.registry.unregister(task_id)
            output_file.unlink(missing_ok=True)
            raise

        if background:
            return _background_output(item, "requested")
        try:
            try:
                snapshot = await asyncio.wait_for(
                    asyncio.shield(handle.wait()), timeout=foreground_budget
                )
            except TimeoutError:
                snapshot = handle.snapshot()
                if not snapshot.status.terminal:
                    self.registry.register(item)
                    latest = handle.snapshot()
                    if latest.status.terminal:
                        self.registry.terminal(latest)
                    return _background_output(item, "timeout")
                snapshot = handle.snapshot()
        except asyncio.CancelledError:
            await handle.cancel("Foreground Bash wait was cancelled.")
            output_file.unlink(missing_ok=True)
            raise
        return _foreground_output(snapshot.status, details, output_file)


def _background_output(item: BackgroundTask, reason: str) -> ToolOutput:
    payload: JsonObject = {
        "task_id": item.task_id,
        "task_type": "bash",
        "status": "running",
        "background_reason": reason,
        "output_file": item.details["output_file"],
    }
    return ToolOutput(json.dumps(payload, ensure_ascii=False), metadata=payload)


def _foreground_output(
    status: TaskStatus, details: JsonObject, output_file: Path
) -> ToolOutput:
    try:
        raw = output_file.read_bytes()
    finally:
        output_file.unlink(missing_ok=True)
    text = raw.decode("utf-8", errors="replace") or "<no output>"
    exit_code = details.get("exit_code")
    if not isinstance(exit_code, int):
        exit_code = 0 if status is TaskStatus.SUCCEEDED else -1
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    failure_hint = ""
    if status is not TaskStatus.SUCCEEDED and details.get("authority") == "use_default":
        capability = (
            " Explicit sandbox escalation is available when genuinely required."
            if details.get("escalation_available") is True
            else " Sandbox escalation is unavailable in this context."
        )
        failure_hint = (
            f"\nExecution backend: {details.get('execution_backend', 'unknown')}."
            f"{capability} The command may have partially executed; inspect side "
            "effects before making a new explicit request."
        )
    return ToolOutput(
        content=f"exit_code: {exit_code}\n{text}{failure_hint}",
        is_error=status is not TaskStatus.SUCCEEDED,
        metadata={
            "exit_code": exit_code,
            "preview": lines[0] if lines else "no output",
            "has_more_output": len(lines) > 1,
        },
    )


__all__ = ["BashBackgroundController"]
