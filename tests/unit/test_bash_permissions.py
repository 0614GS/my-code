from pathlib import Path

import pytest

from my_code.tools.builtin.bash.permissions import (
    analyze_bash_command,
    bash_rule_matches,
)


@pytest.mark.parametrize(
    "command",
    [
        "pwd",
        "ls -la",
        "cat README.md",
        "head -n 20 src/my_code/tools/base.py",
        "rg 'PermissionPolicy' src tests",
        "find src -name '*.py' -type f -print",
        "git status --short",
        "git diff --stat",
        "git log --oneline -n 5",
        "cat README.md | grep nano",
    ],
)
def test_common_workspace_inspection_is_read_only(tmp_path: Path, command: str) -> None:
    assert analyze_bash_command(command, tmp_path).is_read_only is True


@pytest.mark.parametrize(
    "command",
    [
        "rm README.md",
        "cat ../secret.txt",
        "find . -delete",
        "cat README.md > copy.md",
        "echo $(id)",
        "ls *.py",
        "git add README.md",
        "git branch new-branch",
        "rg --pre ./filter pattern .",
        "git status && rm README.md",
    ],
)
def test_unproven_or_mutating_commands_require_approval(
    tmp_path: Path, command: str
) -> None:
    assert analyze_bash_command(command, tmp_path).is_read_only is False


def test_prefix_rules_match_command_boundaries_only() -> None:
    assert bash_rule_matches("git:*", "git status") is True
    assert bash_rule_matches("git:*", "git") is True
    assert bash_rule_matches("git:*", "github status") is False
    assert bash_rule_matches("git status", "git status --short") is False
