"""Execute an explicitly permitted shell command in the workspace."""

import asyncio
import os
import signal

from nano_code.messages import JsonObject
from nano_code.tools.base import (
    Tool,
    ToolContext,
    ToolDefinition,
    ToolExecutionError,
    ToolOutput,
    ToolRisk,
)
from nano_code.tools.validation import optional_int, required_string


class BashTool(Tool):
    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="Bash",
            description=(
                "Run a shell command in the workspace. Commands are permission-gated "
                "but are not OS-sandboxed."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "timeout": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 600,
                        "description": "Timeout in seconds",
                    },
                },
                "required": ["command"],
                "additionalProperties": False,
            },
        )

    @property
    def risk(self) -> ToolRisk:
        return ToolRisk.EXECUTE

    def validate_input(self, tool_input: JsonObject) -> None:
        command = required_string(tool_input, "command")
        if len(command) > 50_000:
            raise ValueError("'command' exceeds 50,000 characters")
        optional_int(tool_input, "timeout", 120, minimum=1, maximum=600)

    async def execute(self, tool_input: JsonObject, context: ToolContext) -> ToolOutput:
        command = required_string(tool_input, "command")
        timeout = optional_int(
            tool_input,
            "timeout",
            round(context.command_timeout_seconds),
            minimum=1,
            maximum=600,
        )
        process = await asyncio.create_subprocess_shell(
            command,
            cwd=context.cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=os.name != "nt",
        )
        try:
            output = await asyncio.wait_for(
                self._collect_output(process, context.max_command_output_bytes),
                timeout=timeout,
            )
        except TimeoutError as error:
            await self._terminate(process)
            raise ToolExecutionError(f"Command timed out after {timeout}s") from error
        except asyncio.CancelledError:
            await self._terminate(process)
            raise
        except BaseException:
            await self._terminate(process)
            raise

        exit_code = await process.wait()
        text = output.decode("utf-8", errors="replace")
        if not text:
            text = "<no output>"
        return ToolOutput(
            content=f"exit_code: {exit_code}\n{text}",
            is_error=exit_code != 0,
        )

    @staticmethod
    async def _collect_output(process: asyncio.subprocess.Process, limit: int) -> bytes:
        if process.stdout is None:
            raise ToolExecutionError("Shell process has no stdout pipe")
        chunks: list[bytes] = []
        size = 0
        while chunk := await process.stdout.read(64 * 1024):
            size += len(chunk)
            if size > limit:
                raise ToolExecutionError(
                    f"Command output exceeded {limit // (1024 * 1024)} MiB"
                )
            chunks.append(chunk)
        return b"".join(chunks)

    @staticmethod
    async def _terminate(process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        try:
            if os.name != "nt":
                os.killpg(process.pid, signal.SIGTERM)
            else:
                process.terminate()
            await asyncio.wait_for(process.wait(), timeout=2)
        except (ProcessLookupError, TimeoutError):
            if process.returncode is None:
                process.kill()
                await process.wait()
