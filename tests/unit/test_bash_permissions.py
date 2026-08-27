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
        "find src -type f | head -100",
        "find src -type f | sort",
        "ls src 2>/dev/null",
        "wc -l *.ts",
        "sed -n '1,20p' src/example.py",
        "echo divider",
    ],
)
def test_observed_read_only_shell_forms_are_allowed(
    tmp_path: Path, command: str
) -> None:
    assert analyze_bash_command(command, tmp_path).is_read_only is True


def test_exact_current_directory_prefix_is_permission_neutral(tmp_path: Path) -> None:
    command = f"cd {tmp_path} && git status --short"
    analysis = analyze_bash_command(command, tmp_path)

    assert analysis.is_read_only is True
    assert analysis.commands == ("git status --short",)
    assert analysis.match_commands == (f"cd {tmp_path}", "git status --short")


@pytest.mark.parametrize(
    "template",
    [
        "cd {cwd}; git status",
        "cd {cwd}/other && git status",
        "cd $PWD && git status",
        "cd {cwd} && rm README.md",
        "sort -o result.txt README.md",
        "sed -n '1,20w output.txt' README.md",
        "rg *.py",
    ],
)
def test_similar_but_unproven_shell_forms_still_require_approval(
    tmp_path: Path, template: str
) -> None:
    command = template.format(cwd=tmp_path)
    assert analyze_bash_command(command, tmp_path).is_read_only is False


@pytest.mark.parametrize(
    "command",
    [
        "rm README.md",
        "cat ../secret.txt",
        "find . -delete",
        "cat README.md > copy.md",
        "echo $(id)",
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
