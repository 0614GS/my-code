import asyncio
from pathlib import Path

import pytest

from nano_code.context.attachments.models import ContextObservation
from nano_code.features.file_mentions import (
    AttachmentLoader,
    WorkspaceAttachmentReader,
    WorkspacePathSuggester,
    parse_file_mentions,
)
from nano_code.features.file_mentions.reader import WorkspaceAttachment
from nano_code.permissions import (
    PermissionBehavior,
    PermissionPolicy,
    PermissionRule,
)


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


def _loader(cwd: Path, policy: PermissionPolicy | None = None) -> AttachmentLoader:
    return AttachmentLoader(
        WorkspaceAttachmentReader(cwd, policy or PermissionPolicy())
    )


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
    observations = [item.attachment.content[0] for item in loaded]
    assert all(isinstance(item, ContextObservation) for item in observations)
    assert isinstance(observations[0], ContextObservation)
    assert isinstance(observations[1], ContextObservation)
    assert "     2\ttwo" in observations[0].body
    assert "docs/a.txt" in observations[1].body
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


@pytest.mark.asyncio
async def test_loader_uses_latest_permission_rules(tmp_path: Path) -> None:
    (tmp_path / "dynamic.txt").write_text("safe", encoding="utf-8")
    policy = PermissionPolicy()
    loader = _loader(tmp_path, policy)

    assert [item.path for item in await loader.load("@dynamic.txt")] == ["dynamic.txt"]
    policy.add_rules((PermissionRule("Read", PermissionBehavior.DENY, "dynamic.txt"),))
    assert await loader.load("@dynamic.txt") == ()


@pytest.mark.asyncio
async def test_directory_listing_is_sorted_bounded_and_skips_external_symlinks(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "many"
    directory.mkdir()
    for index in range(502):
        (directory / f"{501 - index:03}.txt").write_text("x", encoding="utf-8")
    outside = tmp_path.parent / "external-file-mention.txt"
    outside.write_text("secret", encoding="utf-8")
    (directory / "escape.txt").symlink_to(outside)

    loaded = await _loader(tmp_path).load("@many")
    observation = loaded[0].attachment.content[0]

    assert isinstance(observation, ContextObservation)
    lines = observation.body.splitlines()
    assert lines[:2] == ["many/000.txt", "many/001.txt"]
    assert len(lines) == 501
    assert lines[-1] == "<directory listing truncated at 500 entries>"
    assert "escape" not in observation.body


@pytest.mark.asyncio
async def test_default_file_read_is_truncated_at_2000_lines(tmp_path: Path) -> None:
    (tmp_path / "long.txt").write_text(
        "\n".join(str(index) for index in range(2001)), encoding="utf-8"
    )

    loaded = await _loader(tmp_path).load("@long.txt")
    observation = loaded[0].attachment.content[0]

    assert isinstance(observation, ContextObservation)
    assert "  2000\t1999" in observation.body
    assert "  2001\t2000" not in observation.body
    assert "truncated at 2000 lines" in observation.body


@pytest.mark.asyncio
async def test_loader_propagates_reader_cancellation(tmp_path: Path) -> None:
    (tmp_path / "safe.txt").write_text("safe", encoding="utf-8")
    entered = asyncio.Event()
    reader = WorkspaceAttachmentReader(tmp_path, PermissionPolicy())

    async def blocking_read(*args: object, **kwargs: object) -> WorkspaceAttachment:
        del args, kwargs
        entered.set()
        await asyncio.Future()
        raise AssertionError

    reader.read = blocking_read  # type: ignore[method-assign]
    task = asyncio.create_task(AttachmentLoader(reader).load("@safe.txt"))
    await entered.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_loader_keeps_later_mentions_after_unexpected_reader_error(
    tmp_path: Path,
) -> None:
    (tmp_path / "bad.txt").write_text("bad", encoding="utf-8")
    directory = tmp_path / "docs"
    directory.mkdir()
    (directory / "good.txt").write_text("good", encoding="utf-8")
    reader = WorkspaceAttachmentReader(tmp_path, PermissionPolicy())
    real_read = reader.read

    async def sometimes_fail(raw_path: str, **kwargs: object) -> WorkspaceAttachment:
        if raw_path == "bad.txt":
            raise RuntimeError("unexpected read failure")
        return await real_read(raw_path, **kwargs)  # type: ignore[arg-type]

    reader.read = sometimes_fail  # type: ignore[method-assign]
    loaded = await AttachmentLoader(reader).load("@bad.txt @docs")

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
