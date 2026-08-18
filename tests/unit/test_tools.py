import logging
from pathlib import Path

import pytest

from nano_code.agent import ModelToolDefinition
from nano_code.application.chat.presentation import ToolResultPresentation
from nano_code.conversation import JsonObject, ToolCall
from nano_code.core import NanoCodePaths, SettingsScope, SettingsStore
from nano_code.permissions import (
    PermissionBehavior,
    PermissionConfirmation,
    PermissionDecisionKind,
    PermissionDecisionReason,
    PermissionMode,
    PermissionPolicy,
    PermissionRule,
    PermissionUpdate,
    PermissionUpdateDestination,
    ToolPermissionContext,
    ToolPermissionResult,
)
from nano_code.permissions.models import PermissionDecision
from nano_code.permissions.prompt import HeadlessPrompter
from nano_code.permissions.updates import PermissionUpdateApplier
from nano_code.tools import Tool, ToolContext, ToolRegistry
from nano_code.tools.base import ToolOutput
from nano_code.tools.builtin import builtin_tools
from nano_code.tools.executor import ToolExecutor
from nano_code.tools.paths import resolve_workspace_path
from nano_code.tools.result_store import ToolResultStore


def build_executor(tmp_path: Path, mode: PermissionMode) -> ToolExecutor:
    return ToolExecutor(
        registry=ToolRegistry(builtin_tools()),
        policy=PermissionPolicy(mode),
        prompter=HeadlessPrompter(),
        context=ToolContext(cwd=tmp_path),
        result_store=ToolResultStore(tmp_path / ".nano-code" / "results"),
    )


class FeedbackPrompter:
    async def confirm(
        self, tool: Tool, tool_input: JsonObject, decision: PermissionDecision
    ) -> PermissionConfirmation:
        del tool, tool_input, decision
        return PermissionConfirmation(False, "Only explain the proposed change.")


class FailingPrompter:
    async def confirm(
        self, tool: Tool, tool_input: JsonObject, decision: PermissionDecision
    ) -> PermissionConfirmation:
        del tool, tool_input, decision
        raise AssertionError("read-only Bash must not request user confirmation")


class ExplodingPrompter:
    async def confirm(
        self, tool: Tool, tool_input: JsonObject, decision: PermissionDecision
    ) -> PermissionConfirmation:
        del tool, tool_input, decision
        raise RuntimeError("prompt transport failed")


class ApprovingPrompter:
    def __init__(self, *, remember: bool = False) -> None:
        self.remember = remember
        self.calls = 0

    async def confirm(
        self, tool: Tool, tool_input: JsonObject, decision: PermissionDecision
    ) -> PermissionConfirmation:
        del tool, tool_input
        self.calls += 1
        return PermissionConfirmation(
            True,
            updates=decision.suggestions if self.remember else (),
        )


class SessionApprovingPrompter:
    def __init__(self, rule: PermissionRule) -> None:
        self.rule = rule
        self.calls = 0

    async def confirm(
        self, tool: Tool, tool_input: JsonObject, decision: PermissionDecision
    ) -> PermissionConfirmation:
        del tool, tool_input, decision
        self.calls += 1
        return PermissionConfirmation(
            True,
            updates=(
                PermissionUpdate.add_rules(
                    (self.rule,), destination=PermissionUpdateDestination.SESSION
                ),
            ),
        )


class NormalizingTool(Tool):
    @property
    def definition(self) -> ModelToolDefinition:
        return ModelToolDefinition(
            name="Normalize",
            description="Test approved-input propagation.",
            input_schema={"type": "object"},
        )

    def is_read_only(self, tool_input: JsonObject, context: ToolContext) -> bool:
        del tool_input, context
        return False

    def validate_input(self, tool_input: JsonObject) -> None:
        del tool_input

    async def check_permissions(
        self, tool_input: JsonObject, context: ToolPermissionContext
    ) -> ToolPermissionResult:
        del tool_input, context
        return ToolPermissionResult.allow(
            {"value": "approved"},
            message="Normalized input is safe.",
            reason=PermissionDecisionReason(
                PermissionDecisionKind.TOOL, "test-normalized"
            ),
        )

    async def execute(self, tool_input: JsonObject, context: ToolContext) -> ToolOutput:
        del context
        return ToolOutput(str(tool_input["value"]))

    def present_result(
        self, tool_input: JsonObject, output: ToolOutput
    ) -> ToolResultPresentation:
        del tool_input, output
        return ToolResultPresentation(summary="Normalized the approved value")

    def to_model_result(self, output: ToolOutput) -> str:
        return f"model:{output.content}"


class BrokenPresentationTool(NormalizingTool):
    def present_result(
        self, tool_input: JsonObject, output: ToolOutput
    ) -> ToolResultPresentation:
        del tool_input, output
        raise RuntimeError("broken UI projection")


def test_workspace_path_rejects_traversal_but_resolves_sensitive_paths(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="escapes the workspace"):
        resolve_workspace_path(tmp_path, "../outside", writable=True)
    assert resolve_workspace_path(tmp_path, ".git/config", writable=True) == (
        tmp_path / ".git/config"
    )


@pytest.mark.asyncio
async def test_bypass_still_requires_confirmation_for_sensitive_path(
    tmp_path: Path,
) -> None:
    executor = build_executor(tmp_path, PermissionMode.BYPASS)
    outcome = await executor.execute(
        ToolCall(
            id="write-protected",
            name="Write",
            input={"path": ".git/config", "content": "bad"},
        )
    )

    assert outcome.result.is_error is True
    assert "safety:sensitive-workspace-path" in outcome.result.content
    assert "safety:sensitive-workspace-path" in outcome.presentation.summary


@pytest.mark.asyncio
async def test_sensitive_path_can_run_after_one_time_approval(tmp_path: Path) -> None:
    prompter = ApprovingPrompter()
    executor = ToolExecutor(
        registry=ToolRegistry(builtin_tools()),
        policy=PermissionPolicy(PermissionMode.BYPASS),
        prompter=prompter,
        context=ToolContext(cwd=tmp_path),
        result_store=ToolResultStore(tmp_path / "results"),
    )

    outcome = await executor.execute(
        ToolCall(
            id="write-sensitive-approved",
            name="Write",
            input={"path": ".git/config", "content": "approved"},
        )
    )

    assert outcome.result.is_error is False
    assert (tmp_path / ".git/config").read_text(encoding="utf-8") == "approved"
    assert prompter.calls == 1


@pytest.mark.asyncio
async def test_remembered_write_rule_persists_to_local_settings(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    paths = NanoCodePaths(workspace, tmp_path / "config")
    policy = PermissionPolicy()
    prompter = ApprovingPrompter(remember=True)
    executor = ToolExecutor(
        registry=ToolRegistry(builtin_tools()),
        policy=policy,
        prompter=prompter,
        context=ToolContext(cwd=workspace),
        result_store=ToolResultStore(tmp_path / "results"),
        update_applier=PermissionUpdateApplier(policy, SettingsStore(paths)),
    )

    first = await executor.execute(
        ToolCall(
            id="write-remember-1",
            name="Write",
            input={"path": "notes.txt", "content": "first"},
        )
    )
    second = await executor.execute(
        ToolCall(
            id="write-remember-2",
            name="Write",
            input={"path": "notes.txt", "content": "second"},
        )
    )

    local = SettingsStore(paths).load_scope(SettingsScope.LOCAL)
    assert first.result.is_error is False
    assert second.result.is_error is False
    assert prompter.calls == 1
    assert local.permission_allow_rules == ("Write(notes.txt)",)


@pytest.mark.asyncio
async def test_unknown_tool_produces_matching_error_result(tmp_path: Path) -> None:
    executor = build_executor(tmp_path, PermissionMode.DEFAULT)
    outcome = await executor.execute(ToolCall(id="unknown-1", name="Missing", input={}))

    assert outcome.result.tool_use_id == "unknown-1"
    assert outcome.result.is_error is True
    assert "Unknown tool" in outcome.result.content
    assert outcome.presentation.summary == "Unknown tool: Missing"


@pytest.mark.asyncio
async def test_permission_denial_feedback_is_returned_to_model(tmp_path: Path) -> None:
    store = ToolResultStore(tmp_path / ".nano-code" / "results")
    executor = ToolExecutor(
        registry=ToolRegistry(builtin_tools()),
        policy=PermissionPolicy(PermissionMode.DEFAULT),
        prompter=FeedbackPrompter(),
        context=ToolContext(cwd=tmp_path),
        result_store=store,
    )

    outcome = await executor.execute(
        ToolCall(
            id="write-feedback",
            name="Write",
            input={"path": "a.txt", "content": "no"},
        )
    )

    assert outcome.result.is_error is True
    assert "Only explain the proposed change." in outcome.result.content
    assert not (tmp_path / "a.txt").exists()


@pytest.mark.asyncio
async def test_permission_prompt_failure_fails_closed(tmp_path: Path) -> None:
    executor = ToolExecutor(
        registry=ToolRegistry(builtin_tools()),
        policy=PermissionPolicy(PermissionMode.DEFAULT),
        prompter=ExplodingPrompter(),
        context=ToolContext(cwd=tmp_path),
        result_store=ToolResultStore(tmp_path / "results"),
    )

    outcome = await executor.execute(
        ToolCall("write-prompt-error", "Write", {"path": "a.txt", "content": "no"})
    )

    assert outcome.result.is_error is True
    assert "Permission prompt failed (RuntimeError)" in outcome.result.content
    assert not (tmp_path / "a.txt").exists()


@pytest.mark.asyncio
async def test_bash_subprocess_cannot_inherit_provider_api_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-leak")
    monkeypatch.setenv("NANO_CODE_API_KEY", "generic-must-not-leak")
    executor = build_executor(tmp_path, PermissionMode.BYPASS)

    outcome = await executor.execute(
        ToolCall(
            id="bash-secret-boundary",
            name="Bash",
            input={
                "command": (
                    "printf '%s %s' \"${ANTHROPIC_API_KEY-unset}\" "
                    '"${NANO_CODE_API_KEY-unset}"'
                )
            },
        )
    )

    assert outcome.result.is_error is False
    assert "unset" in outcome.result.content
    assert "must-not-leak" not in outcome.result.content
    assert "generic-must-not-leak" not in outcome.result.content


@pytest.mark.asyncio
async def test_read_only_bash_executes_without_permission_prompt(
    tmp_path: Path,
) -> None:
    executor = ToolExecutor(
        registry=ToolRegistry(builtin_tools()),
        policy=PermissionPolicy(PermissionMode.DEFAULT),
        prompter=FailingPrompter(),
        context=ToolContext(cwd=tmp_path),
        result_store=ToolResultStore(tmp_path / ".nano-code" / "results"),
    )

    outcome = await executor.execute(
        ToolCall(id="bash-read-only", name="Bash", input={"command": "pwd"})
    )

    assert outcome.result.is_error is False
    assert str(tmp_path) in outcome.result.content


@pytest.mark.asyncio
async def test_mutating_bash_still_requires_permission(tmp_path: Path) -> None:
    executor = build_executor(tmp_path, PermissionMode.DEFAULT)

    outcome = await executor.execute(
        ToolCall(
            id="bash-write-denied",
            name="Bash",
            input={"command": "printf changed > created.txt"},
        )
    )

    assert outcome.result.is_error is True
    assert "approval was not provided" in outcome.result.content
    assert not (tmp_path / "created.txt").exists()


@pytest.mark.asyncio
async def test_session_allow_rule_removes_later_prompts(tmp_path: Path) -> None:
    rule = PermissionRule(
        "Bash",
        PermissionBehavior.ALLOW,
        "printf:*",
        source="session",
    )
    prompter = SessionApprovingPrompter(rule)
    executor = ToolExecutor(
        registry=ToolRegistry(builtin_tools()),
        policy=PermissionPolicy(PermissionMode.DEFAULT),
        prompter=prompter,
        context=ToolContext(cwd=tmp_path),
        result_store=ToolResultStore(tmp_path / ".nano-code" / "results"),
    )

    first = await executor.execute(
        ToolCall(id="bash-remember-1", name="Bash", input={"command": "printf hello"})
    )
    second = await executor.execute(
        ToolCall(
            id="bash-remember-2",
            name="Bash",
            input={"command": "printf hello world"},
        )
    )

    assert first.result.is_error is False
    assert second.result.is_error is False
    assert prompter.calls == 1


@pytest.mark.asyncio
async def test_explicit_deny_precedes_session_allow(tmp_path: Path) -> None:
    policy = PermissionPolicy(
        PermissionMode.DEFAULT,
        rules=[
            PermissionRule(
                "Bash",
                PermissionBehavior.DENY,
                "printf:*",
                source="localSettings",
            )
        ],
    )
    prompter = SessionApprovingPrompter(
        PermissionRule(
            "Bash",
            PermissionBehavior.ALLOW,
            "printf:*",
            source="session",
        )
    )
    executor = ToolExecutor(
        registry=ToolRegistry(builtin_tools()),
        policy=policy,
        prompter=prompter,
        context=ToolContext(cwd=tmp_path),
        result_store=ToolResultStore(tmp_path / ".nano-code" / "results"),
    )

    outcome = await executor.execute(
        ToolCall(id="bash-denied", name="Bash", input={"command": "printf hello"})
    )

    assert outcome.result.is_error is True
    assert prompter.calls == 0


@pytest.mark.asyncio
async def test_explicit_ask_precedes_session_allow(tmp_path: Path) -> None:
    policy = PermissionPolicy(
        PermissionMode.DEFAULT,
        rules=[
            PermissionRule(
                "Bash",
                PermissionBehavior.ASK,
                "printf:*",
                source="localSettings",
            ),
            PermissionRule(
                "Bash",
                PermissionBehavior.ALLOW,
                "printf:*",
                source="session",
            ),
        ],
    )
    executor = ToolExecutor(
        registry=ToolRegistry(builtin_tools()),
        policy=policy,
        prompter=FeedbackPrompter(),
        context=ToolContext(cwd=tmp_path),
        result_store=ToolResultStore(tmp_path / ".nano-code" / "results"),
    )

    outcome = await executor.execute(
        ToolCall(id="bash-ask-first", name="Bash", input={"command": "printf hello"})
    )

    assert outcome.result.is_error is True
    assert "Only explain the proposed change." in outcome.result.content


@pytest.mark.asyncio
async def test_new_executor_does_not_inherit_session_rules(
    tmp_path: Path,
) -> None:
    rule = PermissionRule(
        "Bash",
        PermissionBehavior.ALLOW,
        "printf:*",
        source="session",
    )

    def build() -> tuple[ToolExecutor, SessionApprovingPrompter]:
        prompter = SessionApprovingPrompter(rule)
        return (
            ToolExecutor(
                registry=ToolRegistry(builtin_tools()),
                policy=PermissionPolicy(PermissionMode.DEFAULT),
                prompter=prompter,
                context=ToolContext(cwd=tmp_path),
                result_store=ToolResultStore(tmp_path / ".nano-code" / "results"),
            ),
            prompter,
        )

    first_executor, first_prompter = build()
    await first_executor.execute(
        ToolCall(id="bash-remember", name="Bash", input={"command": "printf hello"})
    )

    second_executor, second_prompter = build()
    outcome = await second_executor.execute(
        ToolCall(id="bash-fresh", name="Bash", input={"command": "printf hello"})
    )

    assert first_prompter.calls == 1
    assert second_prompter.calls == 1
    assert outcome.result.is_error is False


@pytest.mark.asyncio
async def test_audit_logs_allow_and_denied_ask(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    executor = build_executor(tmp_path, PermissionMode.DEFAULT)

    with caplog.at_level(logging.INFO, logger="nano_code.permissions"):
        await executor.execute(
            ToolCall(id="bash-audit", name="Bash", input={"command": "pwd"})
        )
        await executor.execute(
            ToolCall(
                id="write-audit",
                name="Write",
                input={"path": "a.txt", "content": "denied"},
            )
        )

    records = [record.message for record in caplog.records]
    assert any(
        "behavior=allow" in message and "tool:bash-read-only" in message
        for message in records
    )
    assert any(
        "behavior=deny" in message and "approval was not provided" in message
        for message in records
    )


@pytest.mark.asyncio
async def test_executor_runs_the_exact_input_approved_by_tool_policy(
    tmp_path: Path,
) -> None:
    executor = ToolExecutor(
        registry=ToolRegistry([NormalizingTool()]),
        policy=PermissionPolicy(),
        prompter=FailingPrompter(),
        context=ToolContext(cwd=tmp_path),
        result_store=ToolResultStore(tmp_path / ".nano-code" / "results"),
    )

    outcome = await executor.execute(
        ToolCall(
            id="normalized-input",
            name="Normalize",
            input={"value": "model-provided"},
        )
    )

    assert outcome.result.is_error is False
    assert outcome.result.content == "model:approved"
    assert outcome.presentation == ToolResultPresentation(
        summary="Normalized the approved value"
    )
    assert outcome.result.presentation == outcome.presentation


@pytest.mark.asyncio
async def test_presentation_failure_does_not_change_successful_tool_result(
    tmp_path: Path,
) -> None:
    executor = ToolExecutor(
        registry=ToolRegistry([BrokenPresentationTool()]),
        policy=PermissionPolicy(),
        prompter=FailingPrompter(),
        context=ToolContext(cwd=tmp_path),
        result_store=ToolResultStore(tmp_path / ".nano-code" / "results"),
    )

    outcome = await executor.execute(
        ToolCall(id="broken-presentation", name="Normalize", input={})
    )

    assert outcome.result.is_error is False
    assert outcome.result.content == "model:approved"
    assert outcome.presentation.summary == "approved"
