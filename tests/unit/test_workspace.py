from pathlib import Path

import pytest

from nano_code.context import AgentsUserContextResolver
from nano_code.messages import ContextInstruction, UserContextDocument


def test_agents_resolver_loads_and_wraps_workspace_instructions(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "AGENTS.md").write_text(
        "\n  Use the repository checks.\nKeep the internal newline.  \n\n",
        encoding="utf-8",
    )

    resolved = AgentsUserContextResolver(workspace).resolve()

    assert resolved == (
        UserContextDocument(
            source="AGENTS.md",
            content=(
                ContextInstruction(
                    content=(
                        "As you answer the user's questions, you can use the "
                        "following context:\n"
                        "# AGENTS.md\n"
                        "Use the repository checks.\nKeep the internal newline.\n\n"
                        "IMPORTANT: this context may or may not be relevant to "
                        "your tasks. You should not respond to this context "
                        "unless it is highly relevant to your task."
                    ),
                ),
            ),
        ),
    )


def test_agents_resolver_only_reads_the_workspace_root_file(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (tmp_path / "AGENTS.md").write_text("parent instructions", encoding="utf-8")
    (workspace / "CLAUDE.md").write_text("claude instructions", encoding="utf-8")
    (workspace / "included.md").write_text("included instructions", encoding="utf-8")
    (workspace / "AGENTS.md").write_text(
        "Use this file.\n@include.md", encoding="utf-8"
    )

    resolved = AgentsUserContextResolver(workspace).resolve()

    assert len(resolved) == 1
    block = resolved[0].content[0]
    assert isinstance(block, ContextInstruction)
    assert "Use this file.\n@include.md" in block.content
    assert "<system-reminder>" not in block.content
    assert "parent instructions" not in block.content
    assert "claude instructions" not in block.content
    assert "included instructions" not in block.content


def test_agents_resolver_returns_empty_for_missing_or_blank_file(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    resolver = AgentsUserContextResolver(workspace)

    assert resolver.resolve() == ()

    (workspace / "AGENTS.md").write_text(" \n\t", encoding="utf-8")

    assert resolver.resolve() == ()


def test_agents_resolver_propagates_invalid_file_errors(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    path = workspace / "AGENTS.md"
    resolver = AgentsUserContextResolver(workspace)

    path.mkdir()
    with pytest.raises(IsADirectoryError):
        resolver.resolve()

    path.rmdir()
    path.write_bytes(b"valid prefix\xff")
    with pytest.raises(UnicodeDecodeError):
        resolver.resolve()
