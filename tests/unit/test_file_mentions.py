import asyncio
from pathlib import Path

import pytest

from nano_code.context.attachments.models import AttachmentToolExchange
from nano_code.conversation import JsonObject, ToolCall, ToolResult
from nano_code.features.file_mentions import (
    AttachmentLoader,
    WorkspacePathSuggester,
    parse_file_mentions,
)
from nano_code.permissions import (
    PermissionBehavior,
    PermissionPolicy,
    PermissionRule,
)
from nano_code.permissions.models import PermissionDecision
from nano_code.permissions.prompt import HeadlessPrompter
from nano_code.tools import Tool, ToolContext, ToolRegistry
from nano_code.tools.base import ToolOutput
from nano_code.tools.builtin import builtin_tools
from nano_code.tools.executor import ToolExecutor
from nano_code.tools.invocation import (
    ToolInvocation,
    ToolInvocationAudit,
    ToolInvocationHook,
)
from nano_code.tools.result_store import ToolResultStore


def test_parse_file_mentions_supports_quotes_ranges_and_deduplication() -> None:
    prompt = (
        'inspect @src/main.py#L10-20 and @"docs/path with spaces.md"#L2 '
        "then @src/main.py#L10-20; mail me@example.com"
    )

    mentions = parse_file_mentions(prompt)

    assert [(item.path, item.line_start, item.line_end) for item in mentions] == [
        ("src/main.py", 10, 20),
        ("docs/path with spaces.md", 2, 2),
    ]
    assert mentions[0].raw == "@src/main.py#L10-20"
    assert parse_file_mentions("bad @file#L20-10 and @file#L0") == ()


def _executor(
    cwd: Path,
    policy: PermissionPolicy | None = None,
    *,
    hooks: tuple[ToolInvocationHook, ...] = (),
    audit: ToolInvocationAudit | None = None,
) -> ToolExecutor:
    return ToolExecutor(
        ToolRegistry(builtin_tools()),
        policy or PermissionPolicy(),
        HeadlessPrompter(),
        ToolContext(cwd),
        ToolResultStore(cwd / ".nano-code" / "test-results"),
        hooks=hooks,
        audit=audit,
    )


def _loader(cwd: Path, policy: PermissionPolicy | None = None) -> AttachmentLoader:
    return AttachmentLoader(_executor(cwd, policy))


@pytest.mark.asyncio
async def test_loader_reads_file_range_and_lists_directory(tmp_path: Path) -> None:
    (tmp_path / "sample.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
    directory = tmp_path / "docs"
    directory.mkdir()
    (directory / "a.txt").write_text("a", encoding="utf-8")

    loaded = await _loader(tmp_path).load("use @sample.txt#L2-3 and @docs")

    assert [item.display for item in loaded] == [
        "Read sample.txt",
        "Listed directory docs",
    ]
    exchanges = [item.attachment.content[0] for item in loaded]
    assert all(isinstance(item, AttachmentToolExchange) for item in exchanges)
    assert "     2\ttwo" in exchanges[0].result_content
    assert "docs/a.txt" in exchanges[1].result_content
    assert all(item.attachment.retention == "live_session" for item in loaded)


@pytest.mark.asyncio
async def test_loader_silently_skips_failures_and_keeps_valid_mentions(
    tmp_path: Path,
) -> None:
    (tmp_path / "valid.txt").write_text("valid", encoding="utf-8")
    (tmp_path / "binary.dat").write_bytes(b"a\x00b")
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    (tmp_path / "escape.txt").symlink_to(outside)

    loaded = await _loader(tmp_path).load(
        "@missing.txt @binary.dat @escape.txt @valid.txt"
    )

    assert [item.path for item in loaded] == ["valid.txt"]


@pytest.mark.asyncio
async def test_explicit_mention_accepts_ask_but_never_deny(tmp_path: Path) -> None:
    (tmp_path / "ask.txt").write_text("ask", encoding="utf-8")
    (tmp_path / "deny.txt").write_text("deny", encoding="utf-8")
    policy = PermissionPolicy(
        rules=(
            PermissionRule("Read", PermissionBehavior.ASK, "ask.txt"),
            PermissionRule("Read", PermissionBehavior.DENY, "deny.txt"),
        )
    )

    loaded = await _loader(tmp_path, policy).load("@ask.txt @deny.txt")

    assert [item.path for item in loaded] == ["ask.txt"]


@pytest.mark.asyncio
async def test_explicit_mention_never_overrides_whole_tool_deny(
    tmp_path: Path,
) -> None:
    (tmp_path / "safe.txt").write_text("safe", encoding="utf-8")
    policy = PermissionPolicy(rules=(PermissionRule("Read", PermissionBehavior.DENY),))

    loaded = await _loader(tmp_path, policy).load("@safe.txt")

    assert loaded == ()


class FailingBeforeHook:
    def __init__(self) -> None:
        self.invocation: ToolInvocation | None = None

    async def before_execute(
        self,
        invocation: ToolInvocation,
        call: ToolCall,
        tool: Tool,
        approved_input: JsonObject,
        context: ToolContext,
    ) -> None:
        del call, tool, approved_input, context
        self.invocation = invocation
        raise RuntimeError("hook failed")

    async def after_execute(
        self,
        invocation: ToolInvocation,
        call: ToolCall,
        result: ToolResult,
    ) -> None:
        del invocation, call, result


class FailingAudit:
    async def record_permission(
        self,
        invocation: ToolInvocation,
        call: ToolCall,
        decision: PermissionDecision,
    ) -> None:
        del invocation, call, decision
        raise RuntimeError("audit unavailable")


@pytest.mark.asyncio
async def test_loader_fails_closed_when_invocation_hook_fails(tmp_path: Path) -> None:
    (tmp_path / "safe.txt").write_text("safe", encoding="utf-8")
    hook = FailingBeforeHook()

    loaded = await AttachmentLoader(_executor(tmp_path, hooks=(hook,))).load(
        "@safe.txt"
    )

    assert loaded == ()
    assert hook.invocation == ToolInvocation.explicit_file_mention()


@pytest.mark.asyncio
async def test_loader_fails_closed_when_audit_fails(tmp_path: Path) -> None:
    (tmp_path / "safe.txt").write_text("safe", encoding="utf-8")

    loaded = await AttachmentLoader(_executor(tmp_path, audit=FailingAudit())).load(
        "@safe.txt"
    )

    assert loaded == ()


@pytest.mark.asyncio
async def test_loader_propagates_cancellation_from_invocation(tmp_path: Path) -> None:
    (tmp_path / "safe.txt").write_text("safe", encoding="utf-8")
    entered = asyncio.Event()

    class BlockingHook(FailingBeforeHook):
        async def before_execute(
            self,
            invocation: ToolInvocation,
            call: ToolCall,
            tool: Tool,
            approved_input: JsonObject,
            context: ToolContext,
        ) -> None:
            del invocation, call, tool, approved_input, context
            entered.set()
            await asyncio.Future()

    task = asyncio.create_task(
        AttachmentLoader(_executor(tmp_path, hooks=(BlockingHook(),))).load("@safe.txt")
    )
    await entered.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_loader_keeps_later_mentions_after_unexpected_tool_error(
    tmp_path: Path,
) -> None:
    (tmp_path / "bad.txt").write_text("bad", encoding="utf-8")
    directory = tmp_path / "docs"
    directory.mkdir()
    (directory / "good.txt").write_text("good", encoding="utf-8")
    executor = _executor(tmp_path)
    read = executor.registry.get("Read")
    assert read is not None

    async def fail_execute(tool_input: JsonObject, context: ToolContext) -> ToolOutput:
        del tool_input, context
        raise RuntimeError("unexpected read failure")

    read.execute = fail_execute  # type: ignore[method-assign]

    loaded = await AttachmentLoader(executor).load("@bad.txt @docs")

    assert [item.path for item in loaded] == ["docs"]


@pytest.mark.asyncio
async def test_workspace_suggester_scan_and_ranking_exclude_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("", encoding="utf-8")
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "hidden.py").write_text("", encoding="utf-8")
    suggester = WorkspacePathSuggester(tmp_path)

    assert {(item.path, item.is_directory) for item in suggester._scan_entries()} == {
        ("src", True),
        ("src/main.py", False),
    }

    async def git_paths() -> set[str]:
        return {"src/main.py"}

    monkeypatch.setattr(suggester, "_git_paths", git_paths)
    suggestions = await suggester.suggest("src")

    assert [(item.path, item.is_directory) for item in suggestions] == [
        ("src", True),
        ("src/main.py", False),
    ]
    assert all(".venv" not in item.path for item in await suggester.suggest(""))
