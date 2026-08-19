"""Bash-specific permission analysis and rule orchestration."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from nano_code.permissions.models import PermissionRule
from nano_code.tools.builtin.bash.ast import (
    BashAstResult,
    Redirection,
    SimpleCommand,
    parse_bash,
)
from nano_code.tools.builtin.bash.semantics import command_is_read_only

_SAFE_ENVIRONMENT_NAMES = frozenset({"LANG", "LANGUAGE", "TZ", "NO_COLOR"})
_SAFE_LOCALE_NAME = re.compile(r"LC_[A-Z0-9_]+")


@dataclass(frozen=True, slots=True)
class BashAnalysis:
    """Stable permission facts derived from one Bash input."""

    is_read_only: bool
    reason: str
    commands: tuple[str, ...] = ()
    match_commands: tuple[str, ...] = ()
    ast: BashAstResult | None = None


def analyze_bash_command(command: str, cwd: Path) -> BashAnalysis:
    """Prove the supported static subset read-only; otherwise require approval."""

    ast = parse_bash(command)
    coverage_sources = tuple(item.rule_source for item in ast.commands)
    if not ast.is_complete:
        return BashAnalysis(
            False,
            ast.reason,
            coverage_sources or ast.command_sources,
            ast.command_sources,
            ast,
        )
    if any(not _environment_is_safe(item) for item in ast.commands):
        return BashAnalysis(
            False,
            "environment prefix can alter command behavior",
            coverage_sources,
            ast.command_sources,
            ast,
        )
    redirect_reason = _redirections_allow_read_only(ast.redirections, cwd)
    if redirect_reason is not None:
        return BashAnalysis(
            False, redirect_reason, coverage_sources, ast.command_sources, ast
        )
    for item in ast.commands:
        safe, reason = command_is_read_only(item.argv, cwd)
        if not safe:
            return BashAnalysis(
                False, reason, coverage_sources, ast.command_sources, ast
            )
    return BashAnalysis(
        True,
        "every Bash subcommand is statically proven read-only",
        coverage_sources,
        ast.command_sources,
        ast,
    )


def matching_rule(
    analysis: BashAnalysis, command: str, rules: tuple[PermissionRule, ...]
) -> PermissionRule | None:
    """Match full input and every reliably located command for deny/ask."""

    for rule in rules:
        assert rule.rule_content is not None
        if bash_rule_matches(rule.rule_content, command) or any(
            bash_rule_matches(rule.rule_content, subcommand)
            for subcommand in analysis.match_commands + analysis.commands
        ):
            return rule
    return None


def allowing_rules(
    analysis: BashAnalysis,
    command: str,
    rules: tuple[PermissionRule, ...],
    cwd: Path,
) -> tuple[PermissionRule, ...]:
    """Apply exact whole-input authority or complete per-command coverage."""

    exact = tuple(
        rule
        for rule in rules
        if rule.rule_content is not None
        and _rule_is_exact(rule.rule_content)
        and bash_rule_matches(rule.rule_content, command)
    )
    if exact:
        return exact
    ast = analysis.ast
    if ast is None or not ast.is_complete or not analysis.commands:
        return ()
    if any(not _environment_is_safe(item) for item in ast.commands):
        return ()
    if ast.redirections and any(
        item.argv[0] in {"cd", "pushd", "popd", "source", "."} for item in ast.commands
    ):
        # Redirect paths are resolved against the tool cwd. A shell builtin can
        # change the runtime cwd before a compound redirect is opened.
        return ()
    if not _redirections_are_rule_safe(ast.redirections, cwd):
        return ()

    matched: list[PermissionRule] = []
    for subcommand in analysis.commands:
        rule = next(
            (
                candidate
                for candidate in rules
                if candidate.rule_content is not None
                and bash_rule_matches(candidate.rule_content, subcommand)
            ),
            None,
        )
        if rule is None:
            return ()
        matched.append(rule)
    return tuple(matched)


def bash_rule_matches(rule_content: str, command: str) -> bool:
    """Match exact, ``:*``/`` *`` prefix, or general ``*`` rules."""

    normalized_rule = rule_content.strip()
    normalized_command = command.strip()
    if normalized_rule.endswith(":*"):
        return _prefix_matches(normalized_rule[:-2].rstrip(), normalized_command)
    if normalized_rule.endswith(" *") and not bash_rule_has_wildcard(
        normalized_rule[:-2]
    ):
        return _prefix_matches(normalized_rule[:-2].rstrip(), normalized_command)
    if bash_rule_has_wildcard(normalized_rule):
        if (
            normalized_rule.endswith(" *")
            and _unescaped_star_count(normalized_rule) == 1
        ):
            if normalized_command == normalized_rule[:-2].rstrip():
                return True
        return _wildcard_matches(normalized_rule, normalized_command)
    return normalized_command == _literal_rule_text(normalized_rule)


def bash_rule_has_wildcard(rule_content: str) -> bool:
    pattern = rule_content.strip()
    if pattern.endswith(":*"):
        return False
    return any(
        character == "*" and not _is_escaped(pattern, index)
        for index, character in enumerate(pattern)
    )


def _rule_is_exact(rule_content: str) -> bool:
    normalized = rule_content.rstrip()
    return not normalized.endswith(":*") and not bash_rule_has_wildcard(normalized)


def _environment_is_safe(command: SimpleCommand) -> bool:
    return all(
        item.name in _SAFE_ENVIRONMENT_NAMES
        or _SAFE_LOCALE_NAME.fullmatch(item.name) is not None
        for item in command.environment
    )


def _redirections_allow_read_only(
    redirects: tuple[Redirection, ...], cwd: Path
) -> str | None:
    for redirect in redirects:
        if redirect.kind == "output":
            return "output redirection may write to a file"
        if redirect.kind == "input" and not _safe_redirect_target(redirect.target, cwd):
            return "input redirection references a path outside the workspace"
    return None


def _redirections_are_rule_safe(redirects: tuple[Redirection, ...], cwd: Path) -> bool:
    return all(
        redirect.kind == "fd" or _safe_redirect_target(redirect.target, cwd)
        for redirect in redirects
    )


def _safe_redirect_target(target: str, cwd: Path) -> bool:
    if target == "/dev/null":
        return True
    candidate = Path(target).expanduser()
    if not candidate.is_absolute():
        candidate = cwd / candidate
    try:
        candidate.resolve(strict=False).relative_to(cwd.resolve(strict=False))
    except (OSError, ValueError):
        return False
    return True


def _prefix_matches(prefix: str, command: str) -> bool:
    return command == prefix or command.startswith(prefix + " ")


def _unescaped_star_count(pattern: str) -> int:
    return sum(
        1
        for index, character in enumerate(pattern)
        if character == "*" and not _is_escaped(pattern, index)
    )


def _wildcard_matches(pattern: str, command: str) -> bool:
    regex_parts: list[str] = []
    index = 0
    while index < len(pattern):
        character = pattern[index]
        if character == "\\" and index + 1 < len(pattern):
            following = pattern[index + 1]
            if following in {"*", "\\"}:
                regex_parts.append(re.escape(following))
                index += 2
                continue
        regex_parts.append(".*" if character == "*" else re.escape(character))
        index += 1
    return re.fullmatch("".join(regex_parts), command, flags=re.DOTALL) is not None


def _literal_rule_text(pattern: str) -> str:
    result: list[str] = []
    index = 0
    while index < len(pattern):
        if pattern[index] == "\\" and index + 1 < len(pattern):
            following = pattern[index + 1]
            if following in {"*", "\\"}:
                result.append(following)
                index += 2
                continue
        result.append(pattern[index])
        index += 1
    return "".join(result)


def _is_escaped(pattern: str, index: int) -> bool:
    backslash_count = 0
    cursor = index - 1
    while cursor >= 0 and pattern[cursor] == "\\":
        backslash_count += 1
        cursor -= 1
    return backslash_count % 2 == 1
