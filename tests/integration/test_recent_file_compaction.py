import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from my_code.context.compaction import ContextCompactor
from my_code.context.engine import ContextEngine
from my_code.context.meter import ContextMeter
from my_code.context.planner import ContextPlanner
from my_code.context.rebuild import PostCompactContextRebuilder
from my_code.conversation.attachments import RecentFileSnapshotAttachment
from my_code.conversation.models import HumanMessage
from my_code.model.events import ModelOutputCompleted, ModelStreamEvent
from my_code.model.primitives import ProviderBinding, TokenUsage
from my_code.model.request import (
    ModelOutput,
    ModelRequest,
    ModelTextBlock,
    PromptStability,
)
from my_code.prompts.models import PromptSection
from my_code.prompts.registry import PromptRegistry
from my_code.runtime.recent_files import RuntimeRecentFileRecovery
from my_code.sessions.session import Session
from my_code.tools.base import ToolExecutionContext, ToolExecutionError
from my_code.tools.builtin.edit_file import EditFileTool
from my_code.tools.builtin.read_file import ReadFileTool
from my_code.tools.builtin.write_file import WriteFileTool
from my_code.tools.file_state import FileReadTracker, RecentFileRegistry
from my_code.workspace.local import FileFingerprint, Workspace

SESSION_ID = "11111111-1111-1111-1111-111111111111"
BINDING = ProviderBinding("test", "provider", "model")


class _SummaryModel:
    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
        del request
        yield ModelStreamEvent(
            0,
            ModelOutputCompleted(
                ModelOutput(
                    (ModelTextBlock("Old file descriptions."),),
                    "end_turn",
                    TokenUsage(10, 4, provider_reported=True),
                )
            ),
        )


class _CancellingRecovery(RuntimeRecentFileRecovery):
    async def _current_fingerprint(self, raw_path: Path) -> FileFingerprint | None:
        del raw_path
        raise asyncio.CancelledError


def _components(
    tmp_path: Path,
) -> tuple[
    ContextEngine,
    Workspace,
    ToolExecutionContext,
    FileReadTracker,
    RecentFileRegistry,
]:
    workspace = Workspace(tmp_path)
    reads = FileReadTracker()
    recent = RecentFileRegistry()
    meter = ContextMeter(cache_path=tmp_path / "ratios.json")
    planner = ContextPlanner(
        prompt=PromptRegistry(
            (PromptSection("core", PromptStability.STATIC, lambda: "system"),)
        ),
        max_output_tokens=1000,
        binding_resolver=lambda: BINDING,
        meter=meter,
    )
    recovery = RuntimeRecentFileRecovery(
        workspace, recent, reads, meter, lambda: BINDING
    )
    engine = ContextEngine(
        planner,
        ContextCompactor(_SummaryModel()),
        PostCompactContextRebuilder(),
        recovery,
    )
    tool_context = ToolExecutionContext(
        workspace,
        session_id=SESSION_ID,
        file_reads=reads,
        recent_files=recent,
    )
    return engine, workspace, tool_context, reads, recent


async def _prepare(session: Session, engine: ContextEngine):
    source = session.compaction_input()
    outcome = await engine.compact(source, "manual")
    return source, outcome


@pytest.mark.asyncio
async def test_compact_restores_five_most_recent_current_snapshots(
    tmp_path: Path,
) -> None:
    engine, workspace, tool_context, reads, _ = _components(tmp_path)
    session = Session(tmp_path / "sessions", SESSION_ID)
    session.append_human_message(HumanMessage("work"))
    reader = ReadFileTool()
    paths = tuple(tmp_path / f"file-{index}.txt" for index in range(6))
    for index, path in enumerate(paths):
        path.write_text(f"observed {index}\n", encoding="utf-8")
        await reader.execute({"path": path.name}, tool_context)
    paths[-1].write_text("current newest\n", encoding="utf-8")

    source, outcome = await _prepare(session, engine)
    restored = tuple(
        item
        for item in outcome.attachments
        if isinstance(item, RecentFileSnapshotAttachment)
    )
    assert tuple(item.path for item in restored) == tuple(
        path.name for path in reversed(paths[1:])
    )
    assert restored[0].text == "current newest\n"
    assert all(not item.truncated for item in restored)

    session.commit_compaction(
        outcome.replacements,
        outcome.summary,
        outcome.boundary,
        outcome.attachments,
        source=source,
    )
    await engine.acknowledge_compaction(outcome)

    assert reads.require_complete(SESSION_ID, paths[0]) is None
    for path in paths[1:]:
        assert (
            reads.require_complete(SESSION_ID, path)
            == workspace.read_snapshot(path).fingerprint
        )


@pytest.mark.asyncio
async def test_truncated_restore_authorizes_edit_but_not_full_replacement(
    tmp_path: Path,
) -> None:
    engine, workspace, tool_context, reads, _ = _components(tmp_path)
    session = Session(tmp_path / "sessions", SESSION_ID)
    session.append_human_message(HumanMessage("work"))
    small = tmp_path / "small.txt"
    large = tmp_path / "large.txt"
    small.write_text("small\n", encoding="utf-8")
    large.write_text(
        "".join(f"line {index} " + "x" * 200 + "\n" for index in range(1000)),
        encoding="utf-8",
    )
    await ReadFileTool().execute({"path": small.name}, tool_context)
    await ReadFileTool().execute({"path": large.name, "limit": 5000}, tool_context)

    source, outcome = await _prepare(session, engine)
    restored = tuple(
        item
        for item in outcome.attachments
        if isinstance(item, RecentFileSnapshotAttachment)
    )
    assert [item.path for item in restored] == [large.name, small.name]
    assert restored[0].truncated is True
    assert restored[0].text.endswith("\n")
    assert restored[0].end_line < restored[0].total_lines

    session.commit_compaction(
        outcome.replacements,
        outcome.summary,
        outcome.boundary,
        outcome.attachments,
        source=source,
    )
    await engine.acknowledge_compaction(outcome)
    assert reads.require_complete(SESSION_ID, large) is None
    assert (
        reads.require_observed(SESSION_ID, large)
        == workspace.read_snapshot(large).fingerprint
    )
    assert (
        reads.require_complete(SESSION_ID, small)
        == workspace.read_snapshot(small).fingerprint
    )

    with pytest.raises(ToolExecutionError, match="entire current file"):
        await WriteFileTool().execute(
            {"path": large.name, "content": "replacement\n"}, tool_context
        )
    await EditFileTool().execute(
        {
            "path": large.name,
            "old_string": "line 999 " + "x" * 200,
            "new_string": "changed final line",
        },
        tool_context,
    )
    assert large.read_text(encoding="utf-8").endswith("changed final line\n")


@pytest.mark.asyncio
async def test_changed_after_prepare_is_not_authorized(tmp_path: Path) -> None:
    engine, _, tool_context, reads, _ = _components(tmp_path)
    session = Session(tmp_path / "sessions", SESSION_ID)
    session.append_human_message(HumanMessage("work"))
    path = tmp_path / "note.txt"
    path.write_text("before\n", encoding="utf-8")
    await ReadFileTool().execute({"path": path.name}, tool_context)

    source, outcome = await _prepare(session, engine)
    path.write_text("after\n", encoding="utf-8")
    session.commit_compaction(
        outcome.replacements,
        outcome.summary,
        outcome.boundary,
        outcome.attachments,
        source=source,
    )
    await engine.acknowledge_compaction(outcome)

    assert reads.require_complete(SESSION_ID, path) is None
    assert reads.require_observed(SESSION_ID, path) is None


@pytest.mark.asyncio
async def test_cancelled_compact_acknowledgement_clears_old_observations(
    tmp_path: Path,
) -> None:
    workspace = Workspace(tmp_path)
    reads = FileReadTracker()
    recent = RecentFileRegistry()
    meter = ContextMeter(cache_path=tmp_path / "ratios.json")
    recovery = _CancellingRecovery(workspace, recent, reads, meter, lambda: BINDING)
    context = ToolExecutionContext(
        workspace,
        session_id=SESSION_ID,
        file_reads=reads,
        recent_files=recent,
    )
    path = tmp_path / "note.txt"
    path.write_text("body\n", encoding="utf-8")
    await ReadFileTool().execute({"path": path.name}, context)
    prepared = await recovery.prepare(SESSION_ID, "summary")

    with pytest.raises(asyncio.CancelledError):
        await recovery.acknowledge(prepared.receipt)

    assert reads.require_observed(SESSION_ID, path) is None
    assert reads.require_complete(SESSION_ID, path) is None


@pytest.mark.asyncio
async def test_stale_commit_discards_receipt_and_preserves_old_authorization(
    tmp_path: Path,
) -> None:
    engine, workspace, tool_context, reads, _ = _components(tmp_path)
    session = Session(tmp_path / "sessions", SESSION_ID)
    session.append_human_message(HumanMessage("work"))
    path = tmp_path / "note.txt"
    path.write_text("body\n", encoding="utf-8")
    await ReadFileTool().execute({"path": path.name}, tool_context)
    expected = workspace.read_snapshot(path).fingerprint

    source, outcome = await _prepare(session, engine)
    session.append_human_message(
        HumanMessage("new", parent_uuid=session.causal_head_uuid)
    )
    with pytest.raises(ValueError, match="Stale"):
        session.commit_compaction(
            outcome.replacements,
            outcome.summary,
            outcome.boundary,
            outcome.attachments,
            source=source,
        )
    engine.discard_compaction(outcome)

    assert reads.require_complete(SESSION_ID, path) == expected


@pytest.mark.asyncio
async def test_missing_binary_non_utf8_oversized_and_outside_candidates_are_skipped(
    tmp_path: Path,
) -> None:
    engine, workspace, tool_context, _, recent = _components(tmp_path)
    session = Session(tmp_path / "sessions", SESSION_ID)
    session.append_human_message(HumanMessage("work"))
    good = tmp_path / "good.txt"
    changing = (
        tmp_path / "missing.txt",
        tmp_path / "binary.txt",
        tmp_path / "non-utf8.txt",
        tmp_path / "oversized.txt",
    )
    for path in (*changing, good):
        path.write_text("initial\n", encoding="utf-8")
        await ReadFileTool().execute({"path": path.name}, tool_context)
    changing[0].unlink()
    changing[1].write_bytes(b"binary\x00data")
    changing[2].write_bytes(b"\xff")
    changing[3].write_bytes(b"x" * (8 * 1024 * 1024 + 1))
    outside = tmp_path.parent / "outside-recent.txt"
    outside.write_text("outside\n", encoding="utf-8")
    recent.record(SESSION_ID, outside, workspace.read_snapshot(good).fingerprint)

    _, outcome = await _prepare(session, engine)
    restored = tuple(
        item
        for item in outcome.attachments
        if isinstance(item, RecentFileSnapshotAttachment)
    )

    assert [item.path for item in restored] == [good.name]
