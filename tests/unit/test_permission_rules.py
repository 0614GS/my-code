import pytest

from nano_code.permissions.rules import (
    parse_permission_rule,
    permission_rule_to_string,
    validate_bash_rule_content,
    validate_permission_rule,
)
from nano_code.tools.builtin.bash.permissions import (
    bash_rule_has_wildcard,
    bash_rule_matches,
)


def test_parse_whole_tool_rule() -> None:
    assert parse_permission_rule("Bash") == ("Bash", None)
    assert parse_permission_rule("Read") == ("Read", None)


def test_parse_content_rule_and_normalize_whole_tool_forms() -> None:
    assert parse_permission_rule("Bash(git diff)") == ("Bash", "git diff")
    assert parse_permission_rule("Bash()") == ("Bash", None)
    assert parse_permission_rule("Bash(*)") == ("Bash", None)


def test_parse_escapes_parentheses_and_backslashes() -> None:
    rule = r'Bash(python -c "print\(1\)")'
    assert parse_permission_rule(rule) == (
        "Bash",
        'python -c "print(1)"',
    )
    assert parse_permission_rule(r"Bash(echo \\value)") == (
        "Bash",
        r"echo \value",
    )


def test_parse_preserves_escaped_star_for_shell_matching() -> None:
    assert parse_permission_rule(r"Bash(echo \*)") == ("Bash", r"echo \*")


@pytest.mark.parametrize(
    "rule",
    [
        "",
        "   ",
        "Bash(foo",
        "Bash(foo)bar",
        "(foo)",
        "Bash( )",
        "Bash )",
    ],
)
def test_parse_rejects_malformed_rules(rule: str) -> None:
    with pytest.raises(ValueError, match="Permission rule|Malformed"):
        parse_permission_rule(rule)


def test_validate_preserves_unknown_tools_and_non_bash_content() -> None:
    assert validate_permission_rule("mcp__github") == ("mcp__github", None)
    assert validate_permission_rule("Read(README.md)") == (
        "Read",
        "README.md",
    )


def test_validate_accepts_builtin_tools_and_normalizes_star() -> None:
    assert validate_permission_rule("Bash(git diff)") == ("Bash", "git diff")
    assert validate_permission_rule("Read") == ("Read", None)
    assert validate_permission_rule("Bash(*)") == ("Bash", None)


def test_rule_to_string_round_trips_escaped_content() -> None:
    content = r'echo "a\b(c)"'
    rendered = permission_rule_to_string("Bash", content)
    assert rendered == r'Bash(echo "a\\b\(c\)")'
    assert parse_permission_rule(rendered) == ("Bash", content)


def test_bash_prefix_content_rejects_empty_and_whole_tool() -> None:
    with pytest.raises(ValueError, match="cannot be blank"):
        validate_bash_rule_content("  ")
    with pytest.raises(ValueError, match="bare wildcard"):
        validate_bash_rule_content("*")
    assert validate_bash_rule_content("git diff:*") == "git diff:*"


def test_bash_rule_matches_exact_and_legacy_prefixes() -> None:
    assert bash_rule_matches("git status", "git status") is True
    assert bash_rule_matches("git status", "git status --short") is False
    assert bash_rule_matches("git:*", "git") is True
    assert bash_rule_matches("git:*", "git status") is True
    assert bash_rule_matches("git:*", "github status") is False
    assert bash_rule_matches("git *", "git") is True
    assert bash_rule_matches("git *", "git status") is True


def test_bash_rule_matches_wildcards_and_escaped_star() -> None:
    assert bash_rule_matches("git status*", "git status") is True
    assert bash_rule_matches("git status*", "git status --short") is True
    assert bash_rule_matches("git status*", "git stash") is False
    assert bash_rule_matches("*test*", "unit test") is True
    assert bash_rule_matches("*", "anything") is True
    assert bash_rule_matches(r"echo \*", "echo *") is True
    assert bash_rule_matches(r"echo \*", "echo anything") is False


def test_bash_wildcard_detection_treats_escaped_star_as_literal() -> None:
    assert bash_rule_has_wildcard("git *") is True
    assert bash_rule_has_wildcard("git status*") is True
    assert bash_rule_has_wildcard("git:*") is False
    assert bash_rule_has_wildcard(r"echo \*") is False
