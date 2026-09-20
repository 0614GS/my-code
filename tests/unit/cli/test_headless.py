import io
import json
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from my_code.application.contracts.events import (
    TextDelta,
    TextStarted,
    ToolFinished,
    ToolStarted,
    TurnEvent,
    TurnSucceeded,
)
from my_code.application.service import ApplicationService
from my_code.cli.arguments import OutputFormat, RunCliOptions
from my_code.cli.headless import read_prompt, run_headless
from my_code.config.settings import SandboxMode, SandboxNetwork, SettingsOverrides
from my_code.conversation.presentation import ToolResultPresentation
from my_code.permissions.models import PermissionMode
from my_code.tools.presentation import ToolUsePresentation


class FakeApplication:
    def __init__(self, tmp_path: Path, events: tuple[TurnEvent, ...]) -> None:
        invocation = SimpleNamespace(
            started=SimpleNamespace(invocation_id="invocation-1")
        )
        session = SimpleNamespace(
            session_id="session-1",
            run_id="run-1",
            invocation_history=(invocation,),
        )
        permissions = SimpleNamespace(
            execution_environment="local",
            sandbox_active=False,
        )
        self.runtime = SimpleNamespace(session=session, permissions=permissions)
        self.settings = SimpleNamespace(
            sandbox_mode=SandboxMode.LOCAL,
            sandbox_network=SandboxNetwork.RESTRICTED,
            paths=SimpleNamespace(project_state_dir=tmp_path / "state"),
        )
        self.events = events
        self.closed = False

    def status(self) -> SimpleNamespace:
        return SimpleNamespace(
            session_id="session-1",
            cwd="/workspace",
            provider_id="test",
            model="test-model",
            permission_mode="dontAsk",
        )

    def execution_artifacts(self) -> SimpleNamespace:
        state = self.settings.paths.project_state_dir
        return SimpleNamespace(
            session_log=str(state / "session-record"),
            request_audit_log=str(state / "request-audit-record"),
            diagnostics_directory=str(state / "diagnostics"),
        )

    async def stream(
        self, prompt: str, *, cancellation_message: str = ""
    ) -> AsyncIterator[TurnEvent]:
        del cancellation_message
        assert prompt == "do work"
        for event in self.events:
            yield event

    async def close(self) -> None:
        self.closed = True


class SlowApplication(FakeApplication):
    async def stream(
        self, prompt: str, *, cancellation_message: str = ""
    ) -> AsyncIterator[TurnEvent]:
        import asyncio

        assert "non-interactive host" in cancellation_message
        assert prompt == "do work"
        await asyncio.sleep(1)
        if False:
            yield TextStarted()


def options(
    tmp_path: Path,
    output_format: OutputFormat,
    *,
    timeout_seconds: float | None = None,
) -> RunCliOptions:
    return RunCliOptions(
        cwd=tmp_path,
        session_id=None,
        settings_overrides=SettingsOverrides(permission_mode=PermissionMode.DONT_ASK),
        prompt="do work",
        output_format=output_format,
        timeout_seconds=timeout_seconds,
        dangerously_skip_permissions=False,
    )


def successful_events() -> tuple[TurnEvent, ...]:
    return (
        TextStarted(),
        TextDelta("done"),
        ToolStarted(
            "tool-1",
            ToolUsePresentation("Read", "a.py", "Reading", "explore"),
            "Read",
            {"path": "a.py"},
        ),
        ToolFinished(
            "tool-1",
            False,
            ToolResultPresentation("Read 10 lines"),
        ),
        TurnSucceeded(
            "done",
            2,
            10,
            3,
            4,
            5,
            True,
            "session-1",
            "run-1",
            "invocation-1",
        ),
    )


def as_application(application: FakeApplication) -> ApplicationService:
    return cast(ApplicationService, application)


def test_read_prompt_prefers_argument_and_accepts_piped_stdin(tmp_path: Path) -> None:
    direct = options(tmp_path, OutputFormat.TEXT)
    piped = RunCliOptions(
        cwd=tmp_path,
        session_id=None,
        settings_overrides=direct.settings_overrides,
        prompt=None,
        output_format=OutputFormat.TEXT,
        timeout_seconds=None,
        dangerously_skip_permissions=False,
    )

    assert read_prompt(direct, io.StringIO("ignored")) == "do work"
    assert read_prompt(piped, io.StringIO("from stdin\n")) == "from stdin\n"


@pytest.mark.asyncio
async def test_text_output_contains_only_final_answer(tmp_path: Path) -> None:
    application = FakeApplication(tmp_path, successful_events())
    stdout = io.StringIO()
    stderr = io.StringIO()

    code = await run_headless(
        as_application(application),
        options(tmp_path, OutputFormat.TEXT),
        "do work",
        stdout=stdout,
        stderr=stderr,
    )

    assert code == 0
    assert stdout.getvalue() == "done\n"
    assert stderr.getvalue() == ""
    assert application.closed is True


@pytest.mark.asyncio
async def test_json_output_reports_cache_usage_and_artifacts(tmp_path: Path) -> None:
    application = FakeApplication(tmp_path, successful_events())
    stdout = io.StringIO()

    code = await run_headless(
        as_application(application),
        options(tmp_path, OutputFormat.JSON),
        "do work",
        stdout=stdout,
    )

    result = json.loads(stdout.getvalue())
    assert code == 0
    assert result["outcome"] == "succeeded"
    assert result["invocation_id"] == "invocation-1"
    assert result["usage"] == {
        "input_tokens": 10,
        "cache_creation_input_tokens": 4,
        "cache_read_input_tokens": 5,
        "total_input_tokens": 19,
        "output_tokens": 3,
        "provider_reported": True,
    }
    assert result["artifacts"]["session_log"].endswith("session-record")


@pytest.mark.asyncio
async def test_stream_json_has_ordered_events_and_one_terminal_result(
    tmp_path: Path,
) -> None:
    application = FakeApplication(tmp_path, successful_events())
    stdout = io.StringIO()

    code = await run_headless(
        as_application(application),
        options(tmp_path, OutputFormat.STREAM_JSON),
        "do work",
        stdout=stdout,
    )

    records = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert code == 0
    assert records[0]["type"] == "system"
    assert [record["sequence"] for record in records] == list(range(len(records)))
    assert [record["type"] for record in records].count("result") == 1
    assert records[-1]["type"] == "result"
    assert records[-1]["outcome"] == "succeeded"
    assert {record.get("event") for record in records} >= {
        "text.started",
        "text.delta",
        "tool.started",
        "tool.finished",
    }


@pytest.mark.asyncio
async def test_timeout_emits_terminal_result_and_closes_application(
    tmp_path: Path,
) -> None:
    application = SlowApplication(tmp_path, ())
    stdout = io.StringIO()

    code = await run_headless(
        as_application(application),
        options(tmp_path, OutputFormat.JSON, timeout_seconds=0.001),
        "do work",
        stdout=stdout,
    )

    result = json.loads(stdout.getvalue())
    assert code == 124
    assert result["outcome"] == "timed_out"
    assert result["error"]["code"] == "timeout"
    assert application.closed is True
