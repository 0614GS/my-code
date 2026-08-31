from pathlib import Path

import pytest

from my_code.tools.builtin.bash.permissions import (
    BashEffect,
    analyze_bash_command,
    bash_rule_matches,
    suggest_bash_permission,
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
        "find src -type f | head -100",
        "find src -type f | sort",
        "ls src 2>/dev/null",
        "wc -l *.ts",
        "sed -n '1,20p' src/example.py",
        "echo divider",
    ],
)
def test_supported_workspace_inspection_is_read_only(
    tmp_path: Path, command: str
) -> None:
    assert analyze_bash_command(command, tmp_path).is_read_only is True


def test_exact_current_directory_prefix_is_permission_neutral(tmp_path: Path) -> None:
    command = f"cd {tmp_path} && git status --short"
    analysis = analyze_bash_command(command, tmp_path)

    assert analysis.is_read_only is True
    assert analysis.commands == ("git status --short",)
    assert analysis.match_commands == (f"cd {tmp_path}", "git status --short")


def test_git_c_current_workspace_is_read_only(tmp_path: Path) -> None:
    command = (
        f'git -C {tmp_path} status --short && echo "---BRANCH---" '
        f"&& git -C {tmp_path} branch --show-current"
    )

    analysis = analyze_bash_command(command, tmp_path)

    assert analysis.is_read_only is True


@pytest.mark.parametrize(
    "template",
    [
        "cd {cwd}; git status",
        "cd {cwd}/other && git status",
        "cd $PWD && git status",
        "cd {cwd} && rm README.md",
        "git -C {outside} status --short",
        "git -C {cwd} add README.md",
        "git -C {cwd} -C {cwd} status --short",
        "sort -o result.txt README.md",
        "sed -n '1,20w output.txt' README.md",
        "rg *.py",
    ],
)
def test_similar_but_unproven_shell_forms_still_require_approval(
    tmp_path: Path, template: str
) -> None:
    command = template.format(cwd=tmp_path, outside=tmp_path.parent)
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


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("git push origin main", "git push:*"),
        ("uv run pytest -q", "uv run:*"),
        ("sudo git push", "sudo git push"),
        ("bash -lc 'git push'", "bash -lc 'git push'"),
        ("git status && rm *.tmp", r"git status && rm \*.tmp"),
        ("echo $(id)", "echo $(id)"),
    ],
)
def test_remembered_allow_suggestion_is_deterministic_and_narrow(
    tmp_path: Path, command: str, expected: str
) -> None:
    suggestion = suggest_bash_permission(command, tmp_path)

    assert suggestion.scope == expected
    assert suggestion.rule_content == expected


def test_remembered_allow_omits_redundant_workspace_cd(tmp_path: Path) -> None:
    suggestion = suggest_bash_permission(
        f"cd {tmp_path} && git push origin main", tmp_path
    )

    assert suggestion.rule_content == "git push:*"


def test_escaped_exact_suggestion_does_not_become_a_wildcard(tmp_path: Path) -> None:
    suggestion = suggest_bash_permission("rm *.tmp", tmp_path)

    assert bash_rule_matches(suggestion.rule_content, "rm *.tmp") is True
    assert bash_rule_matches(suggestion.rule_content, "rm secrets.tmp") is False


@pytest.mark.parametrize(
    "command",
    [
        "mkdir -p build/output",
        "touch src/new.py",
        "cp src/a.py src/b.py",
        "mv src/a.py src/b.py",
        "rm -r build/output",
        "rmdir build/output",
        "sed -i 's/old/new/g' src/a.py",
        "cat README.md > build/readme.txt",
        "git status && touch build/stamp",
    ],
)
def test_static_workspace_file_commands_have_workspace_edit_effect(
    tmp_path: Path, command: str
) -> None:
    assert analyze_bash_command(command, tmp_path).effect is BashEffect.WORKSPACE_EDIT


@pytest.mark.parametrize(
    "command",
    [
        "touch $TARGET",
        "touch $(pwd)/stamp",
        "sudo touch build/stamp",
        "cp /etc/passwd copied.txt",
        "rm -r .",
        "rm .git/config",
        "mkdir ../outside",
        "rm *.tmp",
        "sed -i -e 's/old/new/' src/a.py",
        "cat README.md > .my-code/settings.json",
        "git status && curl example.com",
    ],
)
def test_unproven_or_sensitive_edits_have_unknown_effect(
    tmp_path: Path, command: str
) -> None:
    assert analyze_bash_command(command, tmp_path).effect is BashEffect.UNKNOWN
