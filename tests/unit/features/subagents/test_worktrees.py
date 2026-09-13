"""Subagent worktree 创建、保留和清理策略测试。"""

import subprocess
from pathlib import Path

from my_code.features.subagents.worktrees import SubagentWorktreeManager


def _git(cwd: Path, *arguments: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *arguments], check=True)


def _repository(tmp_path: Path) -> Path:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "-q")
    _git(repository, "config", "user.name", "Test")
    _git(repository, "config", "user.email", "test@example.com")
    (repository / "tracked.txt").write_text("base\n", encoding="utf-8")
    _git(repository, "add", "tracked.txt")
    _git(repository, "commit", "-qm", "base")
    return repository


def test_clean_worktree_is_removed_with_its_branch(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    lease = SubagentWorktreeManager(repository, tmp_path / "worktrees").create(
        "11111111-1111-1111-1111-111111111111"
    )

    assert (lease.path / "tracked.txt").exists()
    lease.clean_if_unchanged()

    assert not lease.path.exists()
    branches = subprocess.run(
        ["git", "-C", str(repository), "branch", "--list", lease.branch],
        capture_output=True,
        text=True,
        check=True,
    )
    assert branches.stdout == ""


def test_changed_worktree_is_preserved_for_review(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    lease = SubagentWorktreeManager(repository, tmp_path / "worktrees").create(
        "22222222-2222-2222-2222-222222222222"
    )
    (lease.path / "tracked.txt").write_text("changed\n", encoding="utf-8")

    lease.clean_if_unchanged()

    assert lease.path.exists()
    assert lease.has_changes()
