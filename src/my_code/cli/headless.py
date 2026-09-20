"""无头 CLI host 与稳定的机器输出协议。"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, TextIO

from my_code.application.contracts.events import (
    AttachmentLoaded,
    BackgroundInvocationFinished,
    BackgroundInvocationStarted,
    CompactionCompleted,
    CompactionStarted,
    ContextUpdated,
    MaxStepsReached,
    ModelRequestPrepared,
    ModelStepCompleted,
    PlanCompleted,
    PlanDelta,
    PlanStarted,
    ReasoningCompleted,
    ReasoningDelta,
    ReasoningStarted,
    TextCompleted,
    TextDelta,
    TextStarted,
    TodoListUpdated,
    ToolFinished,
    ToolStarted,
    TurnEvent,
    TurnInputAccepted,
    TurnInputFailed,
    TurnSucceeded,
)
from my_code.application.service import ApplicationService
from my_code.cli.arguments import OutputFormat, RunCliOptions
from my_code.model.primitives import TokenUsage

SCHEMA_VERSION = 1


def read_prompt(options: RunCliOptions, stdin: TextIO | None = None) -> str:
    """Resolve one prompt without ever waiting for interactive input."""

    actual_stdin = sys.stdin if stdin is None else stdin
    if options.prompt is not None:
        prompt = options.prompt
    elif actual_stdin.isatty():
        raise ValueError("mycode run requires a prompt argument or piped stdin")
    else:
        prompt = actual_stdin.read()
    if not prompt.strip():
        raise ValueError("prompt must not be empty")
    return prompt


async def run_headless(
    application: ApplicationService,
    options: RunCliOptions,
    prompt: str,
    *,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    """Execute one turn and emit exactly one terminal machine result."""

    actual_stdout = sys.stdout if stdout is None else stdout
    actual_stderr = sys.stderr if stderr is None else stderr
    started = clock()
    sequence = 0
    terminal: TurnSucceeded | MaxStepsReached | None = None
    failure: tuple[str, str] | None = None
    outcome = "failed"
    exit_code = 1

    if options.output_format is OutputFormat.STREAM_JSON:
        _write_json(actual_stdout, _system_record(application, sequence))
        sequence += 1

    try:
        async with asyncio.timeout(options.timeout_seconds):
            async for event in application.stream(
                prompt,
                cancellation_message=(
                    "Tool execution was aborted by the non-interactive host."
                ),
            ):
                if isinstance(event, (TurnSucceeded, MaxStepsReached)):
                    terminal = event
                    continue
                if options.output_format is OutputFormat.STREAM_JSON:
                    _write_json(
                        actual_stdout, _event_record(event, sequence, application)
                    )
                    sequence += 1
        if isinstance(terminal, TurnSucceeded):
            outcome = "succeeded"
            exit_code = 0
        elif isinstance(terminal, MaxStepsReached):
            outcome = "max_steps"
            exit_code = 3
        else:
            failure = ("missing_terminal", "Agent stream ended without a result")
    except TimeoutError:
        outcome = "timed_out"
        exit_code = 124
        failure = ("timeout", "Agent run exceeded its wall-clock timeout")
    except asyncio.CancelledError:
        outcome = "cancelled"
        exit_code = 130
        failure = ("cancelled", "Agent run was cancelled")
    except Exception as error:
        failure = (type(error).__name__, str(error))

    try:
        await application.close()
    except Exception as error:
        outcome = "failed"
        exit_code = 1
        if failure is None:
            failure = ("shutdown_error", str(error))

    result = _result_record(
        application,
        sequence,
        outcome,
        terminal,
        failure,
        (clock() - started) * 1000,
    )
    if options.output_format is OutputFormat.TEXT:
        if isinstance(terminal, TurnSucceeded) and exit_code == 0:
            actual_stdout.write(terminal.text)
            if not terminal.text.endswith("\n"):
                actual_stdout.write("\n")
            actual_stdout.flush()
        elif failure is not None:
            print(f"Error: {failure[1]}", file=actual_stderr)
        elif isinstance(terminal, MaxStepsReached):
            print(
                f"Error: reached max steps ({terminal.max_steps})",
                file=actual_stderr,
            )
    else:
        _write_json(actual_stdout, result)
    return exit_code


def startup_failure_record(error: Exception) -> dict[str, Any]:
    """Build a protocol result when application assembly never completed."""

    return {
        "schema_version": SCHEMA_VERSION,
        "type": "result",
        "sequence": 0,
        "timestamp": _timestamp(),
        "outcome": "failed",
        "session_id": None,
        "run_id": None,
        "invocation_id": None,
        "text": None,
        "completed_steps": None,
        "max_steps": None,
        "duration_ms": 0.0,
        "usage": _usage(TokenUsage()),
        "error": {"code": type(error).__name__, "message": str(error)},
        "execution_environment": None,
        "artifacts": None,
    }


def write_startup_failure(
    options: RunCliOptions,
    error: Exception,
    *,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> None:
    actual_stdout = sys.stdout if stdout is None else stdout
    actual_stderr = sys.stderr if stderr is None else stderr
    if options.output_format is OutputFormat.TEXT:
        print(f"Error: {error}", file=actual_stderr)
    else:
        _write_json(actual_stdout, startup_failure_record(error))


def _system_record(application: ApplicationService, sequence: int) -> dict[str, Any]:
    status = application.status()
    return {
        "schema_version": SCHEMA_VERSION,
        "type": "system",
        "sequence": sequence,
        "timestamp": _timestamp(),
        "session_id": status.session_id,
        "run_id": application.runtime.session.run_id,
        "cwd": status.cwd,
        "provider": status.provider_id,
        "model": status.model,
        "permission_mode": status.permission_mode,
        "execution_environment": _execution_environment(application),
    }


def _event_record(
    event: TurnEvent, sequence: int, application: ApplicationService
) -> dict[str, Any]:
    name, data = _project_event(event)
    return {
        "schema_version": SCHEMA_VERSION,
        "type": "event",
        "sequence": sequence,
        "timestamp": _timestamp(),
        "session_id": application.runtime.session.session_id,
        "event": name,
        "data": data,
    }


def _project_event(event: TurnEvent) -> tuple[str, dict[str, Any]]:
    if isinstance(event, AttachmentLoaded):
        return "attachment.loaded", {
            "path": event.path,
            "is_directory": event.is_directory,
            "display": event.display,
        }
    if isinstance(event, ModelRequestPrepared):
        return "model.request_prepared", {
            "request_id": event.request_id,
            "request_number": event.request_number,
            "purpose": event.purpose,
            "injections": [
                {
                    "audit_id": item.audit_id,
                    "source": item.source,
                    "attachment_kind": item.attachment_kind,
                    "text": item.text,
                }
                for item in event.injections
            ],
        }
    if isinstance(event, TurnInputAccepted):
        return "turn.input_accepted", {
            "input_id": event.input_id,
            "prompt": event.prompt,
        }
    if isinstance(event, TurnInputFailed):
        return "turn.input_failed", {
            "input_id": event.input_id,
            "prompt": event.prompt,
            "error": event.error,
        }
    if isinstance(event, TextStarted):
        return "text.started", {}
    if isinstance(event, TextDelta):
        return "text.delta", {"text": event.text}
    if isinstance(event, TextCompleted):
        return "text.completed", {"text": event.text}
    if isinstance(event, PlanStarted):
        return "plan.started", {}
    if isinstance(event, PlanDelta):
        return "plan.delta", {"text": event.text}
    if isinstance(event, PlanCompleted):
        return "plan.completed", {"plan": event.plan}
    if isinstance(event, ReasoningStarted):
        return "reasoning.started", {"disclosure": event.disclosure}
    if isinstance(event, ReasoningDelta):
        return "reasoning.delta", {
            "disclosure": event.disclosure,
            "part_index": event.part_index,
            "text": event.text,
        }
    if isinstance(event, ReasoningCompleted):
        return "reasoning.completed", {
            "disclosure": event.presentation.disclosure,
            "parts": list(event.presentation.parts),
        }
    if isinstance(event, ModelStepCompleted):
        return "model.step_completed", {
            "step_index": event.step_index,
            "has_tools": event.has_tools,
        }
    if isinstance(event, CompactionStarted):
        return "compaction.started", {"trigger": event.trigger}
    if isinstance(event, CompactionCompleted):
        return "compaction.completed", {
            "trigger": event.trigger,
            "usage": _usage(event.usage),
            "context": _context(event.status),
        }
    if isinstance(event, ToolStarted):
        return "tool.started", {
            "tool_use_id": event.tool_use_id,
            "name": event.name,
            "input": event.input,
            "presentation": {
                "display_name": event.presentation.display_name,
                "summary": event.presentation.summary,
                "activity": event.presentation.activity,
                "category": event.presentation.category,
            },
        }
    if isinstance(event, ToolFinished):
        presentation = event.presentation
        return "tool.finished", {
            "tool_use_id": event.tool_use_id,
            "is_error": event.is_error,
            "presentation": {
                "summary": presentation.summary,
                "detail": presentation.detail,
                "truncated": presentation.truncated,
                "file_diff": _file_diff(presentation.file_diff),
            },
        }
    if isinstance(event, TodoListUpdated):
        return "todo.updated", {
            "todos": [
                {
                    "content": item.content,
                    "status": item.status,
                    "active_form": item.active_form,
                }
                for item in event.todos
            ]
        }
    if isinstance(event, ContextUpdated):
        return "context.updated", _context(event.status)
    if isinstance(event, BackgroundInvocationStarted):
        return "background_invocation.started", {}
    if isinstance(event, BackgroundInvocationFinished):
        return "background_invocation.finished", {"error": event.error}
    raise TypeError(f"Unsupported turn event: {type(event).__name__}")


def _result_record(
    application: ApplicationService,
    sequence: int,
    outcome: str,
    terminal: TurnSucceeded | MaxStepsReached | None,
    failure: tuple[str, str] | None,
    duration_ms: float,
) -> dict[str, Any]:
    session = application.runtime.session
    latest = session.invocation_history[-1] if session.invocation_history else None
    usage = terminal.usage if terminal is not None else TokenUsage()
    text = terminal.text if isinstance(terminal, TurnSucceeded) else None
    completed_steps = terminal.completed_steps if terminal is not None else None
    max_steps = terminal.max_steps if isinstance(terminal, MaxStepsReached) else None
    session_id = terminal.session_id if terminal is not None else None
    run_id = terminal.run_id if terminal is not None else None
    invocation_id = terminal.invocation_id if terminal is not None else None
    return {
        "schema_version": SCHEMA_VERSION,
        "type": "result",
        "sequence": sequence,
        "timestamp": _timestamp(),
        "outcome": outcome,
        "session_id": session_id or session.session_id,
        "run_id": run_id or session.run_id,
        "invocation_id": invocation_id
        or (latest.started.invocation_id if latest is not None else None),
        "text": text,
        "completed_steps": completed_steps,
        "max_steps": max_steps,
        "duration_ms": round(duration_ms, 3),
        "usage": _usage(usage),
        "error": (
            {"code": failure[0], "message": failure[1]} if failure is not None else None
        ),
        "execution_environment": _execution_environment(application),
        "artifacts": _artifacts(application),
    }


def _usage(usage: TokenUsage) -> dict[str, Any]:
    return {
        "input_tokens": usage.input_tokens,
        "cache_creation_input_tokens": usage.cache_creation_input_tokens,
        "cache_read_input_tokens": usage.cache_read_input_tokens,
        "total_input_tokens": usage.total_input_tokens,
        "output_tokens": usage.output_tokens,
        "provider_reported": usage.provider_reported,
    }


def _context(status: Any) -> dict[str, Any]:
    return {
        "reported_base_tokens": status.reported_base_tokens,
        "estimated_delta_tokens": status.estimated_delta_tokens,
        "projected_tokens": status.projected_tokens,
        "reserved_output_tokens": status.reserved_output_tokens,
        "context_entry_count": status.context_entry_count,
        "conversation_entry_count": status.conversation_entry_count,
        "replacement_count": status.replacement_count,
        "compact_count": status.compact_count,
        "input_limit_tokens": status.input_limit_tokens,
        "compact_trigger_tokens": status.compact_trigger_tokens,
        "remaining_input_tokens": status.remaining_input_tokens,
        "measurement": status.measurement,
        "model_limit_source": status.model_limit_source,
        "configured_compact_trigger_tokens": status.configured_compact_trigger_tokens,
        "warning": status.warning,
        "cache_hit_rate": status.cache_hit_rate,
    }


def _file_diff(diff: Any) -> dict[str, Any] | None:
    if diff is None:
        return None
    return {
        "path": diff.path,
        "operation": diff.operation,
        "additions": diff.additions,
        "deletions": diff.deletions,
        "old_ends_with_newline": diff.old_ends_with_newline,
        "new_ends_with_newline": diff.new_ends_with_newline,
        "omitted_lines": diff.omitted_lines,
        "omitted_reason": diff.omitted_reason,
        "hunks": [
            {
                "old_start": hunk.old_start,
                "old_count": hunk.old_count,
                "new_start": hunk.new_start,
                "new_count": hunk.new_count,
                "lines": [
                    {
                        "kind": line.kind,
                        "text": line.text,
                        "old_line": line.old_line,
                        "new_line": line.new_line,
                        "omitted_lines": line.omitted_lines,
                    }
                    for line in hunk.lines
                ],
            }
            for hunk in diff.hunks
        ],
    }


def _execution_environment(application: ApplicationService) -> dict[str, Any]:
    permissions = application.runtime.permissions
    return {
        "display": permissions.execution_environment,
        "sandboxed": permissions.sandbox_active,
        "requested_mode": application.settings.sandbox_mode.value,
        "network": application.settings.sandbox_network.value,
    }


def _artifacts(application: ApplicationService) -> dict[str, str | None]:
    artifacts = application.execution_artifacts()
    return {
        "session_log": artifacts.session_log,
        "request_audit_log": artifacts.request_audit_log,
        "diagnostics_directory": artifacts.diagnostics_directory,
    }


def _write_json(stream: TextIO, value: dict[str, Any]) -> None:
    stream.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
    stream.write("\n")
    stream.flush()


def _timestamp() -> str:
    return datetime.now(UTC).isoformat()


__all__ = [
    "SCHEMA_VERSION",
    "read_prompt",
    "run_headless",
    "startup_failure_record",
    "write_startup_failure",
]
