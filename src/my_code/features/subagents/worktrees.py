"""General Subagent 的 Git worktree 生命周期。"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from my_code.tools.base import ToolExecutionError


@dataclass(frozen=True, slots=True)
class SubagentWorktree:
    path: Path
    branch: str
    repository: Path

    def has_changes(self) -> bool:
        result = _git(self.path, "status", "--porcelain")
        return bool(result.stdout.strip())

    def clean_if_unchanged(self) -> None:
        """有用户可审查改动时保留，否则移除临时 worktree 与分支。"""

        if self.path.exists() and self.has_changes():
            return
        _git(self.repository, "worktree", "remove", "--force", str(self.path))
        _git(self.repository, "branch", "-D", self.branch)


class SubagentWorktreeManager:
    def __init__(self, workspace: Path, root: Path) -> None:
        self.workspace = workspace.resolve()
        self.root = root.resolve(strict=False)

    def create(self, run_id: str) -> SubagentWorktree:
        try:
            probe = _git(self.workspace, "rev-parse", "--show-toplevel")
        except ToolExecutionError as error:
            raise ToolExecutionError(
                "worktree isolation requires a Git workspace"
            ) from error
        repository = Path(probe.stdout.strip()).resolve()
        if repository != self.workspace:
            raise ToolExecutionError(
                "worktree isolation requires the workspace to be a Git root"
            )
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root.chmod(0o700)
        short_id = run_id.replace("-", "")[:12]
        path = self.root / short_id
        branch = f"my-code/subagent-{short_id}"
        try:
            _git(repository, "worktree", "add", "-b", branch, str(path), "HEAD")
        except BaseException:
            shutil.rmtree(path, ignore_errors=True)
            subprocess.run(
                ["git", "-C", str(repository), "branch", "-D", branch],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            raise
        return SubagentWorktree(path, branch, repository)


def _git(cwd: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", "-C", str(cwd), *arguments],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown error"
        raise ToolExecutionError(f"Git worktree operation failed: {detail}")
    return result


__all__ = ["SubagentWorktree", "SubagentWorktreeManager"]
