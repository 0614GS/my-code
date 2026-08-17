from pathlib import Path

import pytest

from nano_code.attachments import (
    AttachmentLoader,
    WorkspacePathSuggester,
    format_path_mention,
    mention_at_cursor,
    parse_file_mentions,
)
from nano_code.messages import AttachmentToolExchange
from nano_code.permissions import (
    PermissionBehavior,
    PermissionPolicy,
    PermissionRule,
)
from nano_code.tools import ToolContext, ToolRegistry
from nano_code.tools.builtin import builtin_tools


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


def test_cursor_mention_and_quoted_insertion() -> None:
    assert mention_at_cursor("read @src/ma.py now", 12) == (5, 15, "src/ma")
    assert mention_at_cursor('read @"docs/main file.md"', 12) == (
        5,
        25,
        "docs/",
    )
    assert mention_at_cursor("me@example.com", 14) is None
    assert format_path_mention("docs/a file.md") == '@"docs/a file.md"'


def _loader(cwd: Path, policy: PermissionPolicy | None = None) -> AttachmentLoader:
    return AttachmentLoader(
        ToolRegistry(builtin_tools()),
        policy or PermissionPolicy(),
        ToolContext(cwd),
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
