from pathlib import Path

import pytest

from nano_code.model import JsonObject
from nano_code.permissions import (
    PermissionBehavior,
    PermissionMode,
    PermissionPolicy,
    PermissionRequest,
    PermissionRule,
    ToolPermissionContext,
)
from nano_code.tools.builtin.bash import BashTool
from nano_code.tools.builtin.bash.ast import parse_bash
from nano_code.tools.builtin.bash.permissions import analyze_bash_command


def test_ast_decodes_static_quotes_concatenation_and_unicode_spans() -> None:
    result = parse_bash("cat '文件 名'\nprintf \"a\"b")

    assert result.is_complete is True
    assert [command.argv for command in result.commands] == [
        ("cat", "文件 名"),
        ("printf", "ab"),
    ]
    assert result.commands[0].source == "cat '文件 名'"
    assert result.commands[1].source == 'printf "a"b'


def test_ast_extracts_compound_commands_environment_and_redirections() -> None:
    result = parse_bash(
        "LANG=C git status && cat README.md | rg nano > result.txt 2>&1"
    )

    assert result.is_complete is True
    assert [command.argv for command in result.commands] == [
        ("git", "status"),
        ("cat", "README.md"),
        ("rg", "nano"),
    ]
    assert result.commands[0].environment[0].name == "LANG"
    assert [(item.kind, item.target) for item in result.redirections] == [
        ("output", "result.txt"),
        ("fd", "1"),
    ]
    assert result.commands[0].redirections == result.redirections
    assert result.commands[1].redirections == result.redirections
    assert result.commands[2].redirections == result.redirections


@pytest.mark.parametrize(
    "command",
    [
        "pwd &",
        "echo $VALUE",
        "ls *.py",
        "echo {a,b}",
        "echo $((1 + 2))",
        "echo $(pwd)",
        "echo <(pwd)",
        "(pwd)",
        "if true; then pwd; fi",
        "f() { pwd; }",
        "cat <<< value",
        "cat <<EOF\nvalue\nEOF\n",
    ],
)
def test_complex_bash_constructs_fail_closed(command: str) -> None:
    assert parse_bash(command).is_complete is False


def test_syntax_errors_fail_closed() -> None:
    result = parse_bash("echo 'unterminated")

    assert result.is_complete is False
    assert "error or missing" in result.reason


def test_hidden_substitution_command_is_reliably_extracted() -> None:
    result = parse_bash("printf '%s' $(rm secret.txt)")

    assert result.is_complete is False
    assert "rm secret.txt" in result.command_sources


def test_ast_character_and_node_budgets_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert parse_bash("x" * 10_001).is_complete is False

    monkeypatch.setattr("nano_code.tools.builtin.bash.ast.MAX_AST_NODES", 2)
    result = parse_bash("git status")
    assert result.is_complete is False
    assert "node limit" in result.reason


def test_input_redirect_must_stay_in_workspace(tmp_path: Path) -> None:
    assert analyze_bash_command("cat < input.txt", tmp_path).is_read_only is True
    assert analyze_bash_command("cat < /etc/passwd", tmp_path).is_read_only is False
    assert analyze_bash_command("cat < /dev/null", tmp_path).is_read_only is True
    assert analyze_bash_command("cat 2>&1", tmp_path).is_read_only is True


def test_output_redirect_is_never_automatically_read_only(tmp_path: Path) -> None:
    assert (
        analyze_bash_command("cat README.md > result.txt", tmp_path).is_read_only
        is False
    )
    assert (
        analyze_bash_command("cat README.md > /dev/null", tmp_path).is_read_only
        is False
    )


def test_only_safe_environment_prefixes_inherit_read_only_semantics(
    tmp_path: Path,
) -> None:
    assert analyze_bash_command("LANG=C git status", tmp_path).is_read_only is True
    assert (
        analyze_bash_command("LC_ALL=C TZ=UTC git status", tmp_path).is_read_only
        is True
    )
    assert analyze_bash_command("PATH=/tmp git status", tmp_path).is_read_only is False
    assert (
        analyze_bash_command("GIT_DIR=../repo git status", tmp_path).is_read_only
        is False
    )


async def _decide(
    tmp_path: Path, command: str, *rules: PermissionRule
) -> PermissionBehavior:
    policy = PermissionPolicy(PermissionMode.DEFAULT, rules=rules)
    tool = BashTool()
    tool_input: JsonObject = {"command": command}
    local = await tool.check_permissions(
        tool_input, ToolPermissionContext(policy.mode, policy.rules, tmp_path)
    )
    decision = policy.decide(PermissionRequest(tool.definition.name, tool_input, local))
    return decision.behavior


@pytest.mark.asyncio
async def test_hidden_command_still_matches_deny_rule(tmp_path: Path) -> None:
    behavior = await _decide(
        tmp_path,
        "printf '%s' $(rm secret.txt)",
        PermissionRule("Bash", PermissionBehavior.DENY, "rm:*"),
    )

    assert behavior is PermissionBehavior.DENY


@pytest.mark.asyncio
async def test_environment_prefix_cannot_hide_command_from_deny_rule(
    tmp_path: Path,
) -> None:
    behavior = await _decide(
        tmp_path,
        "LANG=C git status",
        PermissionRule("Bash", PermissionBehavior.DENY, "git:*"),
    )

    assert behavior is PermissionBehavior.DENY


@pytest.mark.asyncio
async def test_complex_syntax_cannot_use_prefix_allow(tmp_path: Path) -> None:
    behavior = await _decide(
        tmp_path,
        "git status && (git diff)",
        PermissionRule("Bash", PermissionBehavior.ALLOW, "git:*"),
    )

    assert behavior is PermissionBehavior.ASK


@pytest.mark.asyncio
async def test_exact_full_input_rule_can_approve_complex_or_long_input(
    tmp_path: Path,
) -> None:
    complex_command = "printf '%s' $(date)"
    long_command = "printf " + "x" * 10_001

    assert (
        await _decide(
            tmp_path,
            complex_command,
            PermissionRule("Bash", PermissionBehavior.ALLOW, complex_command),
        )
        is PermissionBehavior.ALLOW
    )
    assert (
        await _decide(
            tmp_path,
            long_command,
            PermissionRule("Bash", PermissionBehavior.ALLOW, long_command),
        )
        is PermissionBehavior.ALLOW
    )


@pytest.mark.asyncio
async def test_prefix_rule_can_approve_workspace_output_redirect(
    tmp_path: Path,
) -> None:
    rule = PermissionRule("Bash", PermissionBehavior.ALLOW, "printf:*")

    assert (
        await _decide(tmp_path, "printf x > result.txt", rule)
        is PermissionBehavior.ALLOW
    )
    assert (
        await _decide(tmp_path, "printf x > ../result.txt", rule)
        is PermissionBehavior.ASK
    )


@pytest.mark.asyncio
async def test_redirect_symlink_escape_is_not_approved_by_prefix(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (workspace / "link").symlink_to(outside, target_is_directory=True)

    behavior = await _decide(
        workspace,
        "printf x > link/result.txt",
        PermissionRule("Bash", PermissionBehavior.ALLOW, "printf:*"),
    )

    assert behavior is PermissionBehavior.ASK


@pytest.mark.asyncio
async def test_cwd_change_with_redirect_is_not_approved_by_prefix(
    tmp_path: Path,
) -> None:
    behavior = await _decide(
        tmp_path,
        "cd .. && printf x > result.txt",
        PermissionRule("Bash", PermissionBehavior.ALLOW, "cd:*"),
        PermissionRule("Bash", PermissionBehavior.ALLOW, "printf:*"),
    )

    assert behavior is PermissionBehavior.ASK


@pytest.mark.asyncio
async def test_unsafe_environment_does_not_inherit_prefix_rule(tmp_path: Path) -> None:
    safe = await _decide(
        tmp_path,
        "NO_COLOR=1 printf x",
        PermissionRule("Bash", PermissionBehavior.ALLOW, "printf:*"),
    )
    unsafe = await _decide(
        tmp_path,
        "PYTHONPATH=src printf x",
        PermissionRule("Bash", PermissionBehavior.ALLOW, "printf:*"),
    )

    assert safe is PermissionBehavior.ALLOW
    assert unsafe is PermissionBehavior.ASK
