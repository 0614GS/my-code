"""POSIX Bash process execution and cancellation cleanup."""

from __future__ import annotations

import asyncio
import os
import signal

from my_code.tools.base import ToolContext, ToolExecutionError, ToolOutput

BASH_EXECUTABLE = "/bin/bash"
_REMOVED_ENVIRONMENT = frozenset(
    {
        "MY_CODE_API_KEY",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "BASH_ENV",
        "ENV",
        "SHELLOPTS",
        "BASHOPTS",
        "CDPATH",
    }
)


async def execute_bash(
    command: str, context: ToolContext, timeout_seconds: int
) -> ToolOutput:
    """Execute exactly ``/bin/bash -c`` after permission approval."""

    try:
        process = await asyncio.create_subprocess_exec(
            BASH_EXECUTABLE,
            "-c",
            command,
            cwd=context.cwd,
            env=subprocess_environment(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
    except (FileNotFoundError, PermissionError, OSError) as error:
        raise ToolExecutionError(
            f"Required Bash executable {BASH_EXECUTABLE!r} is unavailable "
            "or not executable"
        ) from error

    try:
        output = await asyncio.wait_for(
            _collect_output(process, context.max_command_output_bytes),
            timeout=timeout_seconds,
        )
    except TimeoutError as error:
        await _terminate(process)
        raise ToolExecutionError(
            f"Command timed out after {timeout_seconds}s"
        ) from error
    except asyncio.CancelledError:
        await _terminate(process)
        raise
    except BaseException:
        await _terminate(process)
        raise

    exit_code = await process.wait()
    text = output.decode("utf-8", errors="replace") or "<no output>"
    output_lines = [line.strip() for line in text.splitlines() if line.strip()]
    return ToolOutput(
        content=f"exit_code: {exit_code}\n{text}",
        is_error=exit_code != 0,
        metadata={
            "exit_code": exit_code,
            "preview": output_lines[0] if output_lines else "no output",
            "has_more_output": len(output_lines) > 1,
        },
    )


def subprocess_environment() -> dict[str, str]:
    environment = {
        name: value
        for name, value in os.environ.items()
        if name not in _REMOVED_ENVIRONMENT and not name.startswith("BASH_FUNC_")
    }
    return environment


async def _collect_output(process: asyncio.subprocess.Process, limit: int) -> bytes:
    if process.stdout is None:
        raise ToolExecutionError("Bash process has no stdout pipe")
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


async def _terminate(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        await asyncio.wait_for(process.wait(), timeout=2)
    except ProcessLookupError:
        await process.wait()
        return
    except TimeoutError:
        pass
    # The direct Bash process may exit before a descendant that ignored TERM.
    # Escalate the original process group even when Bash has already been reaped.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    if process.returncode is None:
        await process.wait()
