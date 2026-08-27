"""Bash-specific permission analysis and rule orchestration."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from my_code.permissions.models import PermissionRule
from my_code.tools.builtin.bash.ast import (
    BashAstResult,
    Redirection,
    SimpleCommand,
    parse_bash,
)
from my_code.tools.builtin.bash.semantics import command_is_read_only
from my_code.tools.paths import is_sensitive_write_path

_SAFE_ENVIRONMENT_NAMES = frozenset({"LANG", "LANGUAGE", "TZ", "NO_COLOR"})
_SAFE_LOCALE_NAME = re.compile(r"LC_[A-Z0-9_]+")


class BashEffect(StrEnum):
    READ_ONLY = "read-only"
    WORKSPACE_EDIT = "workspace-edit"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class BashAnalysis:
    """Stable permission facts derived from one Bash input."""

    effect: BashEffect
    reason: str
    commands: tuple[str, ...] = ()
    match_commands: tuple[str, ...] = ()
    ast: BashAstResult | None = None

    @property
    def is_read_only(self) -> bool:
        return self.effect is BashEffect.READ_ONLY

    @property
    def is_workspace_edit(self) -> bool:
        return self.effect is BashEffect.WORKSPACE_EDIT


@dataclass(frozen=True, slots=True)
class BashPermissionSuggestion:
    """A deterministic, non-broad rule offered by the permission prompt."""

    scope: str
    rule_content: str


_STABLE_SUBCOMMAND_TOOLS = frozenset(
    {"cargo", "docker", "git", "gh", "kubectl", "npm", "pnpm", "uv", "yarn"}
)
_DANGEROUS_WRAPPERS = frozenset(
    {"bash", "dash", "env", "fish", "sh", "sudo", "timeout", "xargs", "zsh"}
)


def analyze_bash_command(command: str, cwd: Path) -> BashAnalysis:
    """Prove the supported static subset read-only; otherwise require approval."""

    ast = parse_bash(command)
    semantic_commands = _semantic_commands(ast, command, cwd)
    coverage_sources = tuple(item.rule_source for item in semantic_commands)
    if not ast.is_complete:
        return BashAnalysis(
            BashEffect.UNKNOWN,
            ast.reason,
            coverage_sources or ast.command_sources,
            ast.command_sources,
            ast,
        )
    if any(not _environment_is_safe(item) for item in semantic_commands):
        return BashAnalysis(
            BashEffect.UNKNOWN,
            "environment prefix can alter command behavior",
            coverage_sources,
            ast.command_sources,
            ast,
        )
    redirect_reason = _redirections_allow_read_only(ast.redirections, cwd)
    read_only_reason = redirect_reason
    for item in semantic_commands:
        if item.unquoted_glob_indices and item.argv[0] not in {
            "wc",
            "ls",
            "cat",
            "head",
            "tail",
            "stat",
            "file",
        }:
            return BashAnalysis(
                BashEffect.UNKNOWN,
                f"unquoted glob arguments are unsupported for {item.argv[0]!r}",
                coverage_sources,
                ast.command_sources,
                ast,
            )
        safe, reason = command_is_read_only(item.argv, cwd)
        if not safe:
            read_only_reason = reason
            break
    else:
        if redirect_reason is None:
            return BashAnalysis(
                BashEffect.READ_ONLY,
                "every Bash subcommand is statically proven read-only",
                coverage_sources,
                ast.command_sources,
                ast,
            )

    edit_reason = _workspace_edit_reason(semantic_commands, ast.redirections, cwd)
    if edit_reason is None:
        return BashAnalysis(
            BashEffect.WORKSPACE_EDIT,
            "every Bash subcommand is read-only or a safe workspace edit",
            coverage_sources,
            ast.command_sources,
            ast,
        )
    return BashAnalysis(
        BashEffect.UNKNOWN,
        edit_reason or read_only_reason or "command effect could not be proven",
        coverage_sources,
        ast.command_sources,
        ast,
    )


_EDIT_FLAGS: dict[str, frozenset[str]] = {
    "mkdir": frozenset({"-p", "--parents"}),
    "touch": frozenset({"-c", "--no-create"}),
    "cp": frozenset({"-a", "-f", "-n", "-p", "-r", "-R", "--recursive", "--force"}),
    "mv": frozenset({"-f", "-n", "--force", "--no-clobber"}),
    "rm": frozenset({"-d", "-f", "-r", "-R", "--dir", "--force", "--recursive"}),
    "rmdir": frozenset({"-p", "--parents", "--ignore-fail-on-non-empty"}),
}


def _workspace_edit_reason(
    commands: tuple[SimpleCommand, ...],
    redirects: tuple[Redirection, ...],
    cwd: Path,
) -> str | None:
    has_edit = False
    for redirect in redirects:
        if redirect.kind == "input":
            if not _safe_redirect_target(redirect.target, cwd):
                return "input redirection references a path outside the workspace"
        elif redirect.kind == "output" and redirect.target != "/dev/null":
            reason = _unsafe_edit_path(redirect.target, cwd)
            if reason is not None:
                return reason
            has_edit = True
    for command in commands:
        read_only, _ = command_is_read_only(command.argv, cwd)
        if read_only:
            continue
        reason = _safe_edit_command(command.argv, cwd)
        if reason is not None:
            return reason
        has_edit = True
    return None if has_edit else "command is not a proven workspace edit"


def _safe_edit_command(argv: tuple[str, ...], cwd: Path) -> str | None:
    name = argv[0]
    if name == "sed":
        return _safe_sed_edit(argv[1:], cwd)
    allowed = _EDIT_FLAGS.get(name)
    if allowed is None:
        return f"command {name!r} is not a supported workspace edit"
    operands: list[str] = []
    options = True
    for argument in argv[1:]:
        if options and argument == "--":
            options = False
        elif options and argument.startswith("-"):
            if argument not in allowed:
                return f"unsupported option {argument!r} for {name!r}"
        else:
            options = False
            operands.append(argument)
    minimum = 2 if name in {"cp", "mv"} else 1
    if len(operands) < minimum:
        return f"{name!r} has too few static path operands"
    for operand in operands:
        reason = _unsafe_edit_path(operand, cwd)
        if reason is not None:
            return reason
    return None


def _safe_sed_edit(arguments: tuple[str, ...], cwd: Path) -> str | None:
    if not arguments:
        return "sed has no arguments"
    index = 0
    in_place = False
    while index < len(arguments) and arguments[index].startswith("-"):
        option = arguments[index]
        if (
            option in {"-i", "--in-place"}
            or option.startswith("-i")
            or option.startswith("--in-place=")
        ):
            in_place = True
        elif option == "--":
            index += 1
            break
        else:
            return f"unsupported sed edit option {option!r}"
        index += 1
    if not in_place or index >= len(arguments):
        return "sed is not a conservative in-place edit"
    program = arguments[index]
    paths = arguments[index + 1 :]
    if not paths or not _safe_sed_substitution(program):
        return "sed program is not a conservative substitution"
    for path in paths:
        reason = _unsafe_edit_path(path, cwd)
        if reason is not None:
            return reason
    return None


def _safe_sed_substitution(program: str) -> bool:
    if len(program) < 4 or program[0] != "s" or program[1].isalnum():
        return False
    delimiter = program[1]
    cursor = 2
    for _ in range(2):
        escaped = False
        while cursor < len(program):
            character = program[cursor]
            cursor += 1
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == delimiter:
                break
        else:
            return False
    return re.fullmatch(r"[gIp0-9]*", program[cursor:]) is not None


def _unsafe_edit_path(value: str, cwd: Path) -> str | None:
    if value in {"", "-"} or any(character in value for character in "*?["):
        return f"dynamic or glob path {value!r} is not a proven workspace path"
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = cwd / candidate
    try:
        resolved = candidate.resolve(strict=False)
        root = cwd.resolve(strict=False)
        relative = resolved.relative_to(root)
    except (OSError, ValueError):
        return f"path {value!r} is outside the workspace"
    if not relative.parts:
        return "the workspace root cannot be an edit target"
    if is_sensitive_write_path(cwd, resolved):
        return f"path {value!r} is sensitive and requires approval"
    return None


def suggest_bash_permission(command: str, cwd: Path) -> BashPermissionSuggestion:
    """Return the remembered-allow scope for one Bash approval request."""

    ast = parse_bash(command)
    semantic = _semantic_commands(ast, command, cwd) if ast.is_complete else ()
    if len(semantic) == 1:
        item = semantic[0]
        if (
            not item.environment
            and not item.redirections
            and len(item.argv) >= 2
            and item.argv[0] in _STABLE_SUBCOMMAND_TOOLS
            and item.argv[0] not in _DANGEROUS_WRAPPERS
            and not item.argv[1].startswith("-")
        ):
            prefix = f"{item.argv[0]} {item.argv[1]}:*"
            return BashPermissionSuggestion(prefix, prefix)

    exact = _command_without_redundant_cwd(command, ast, semantic)
    escaped = _escape_exact_rule(exact)
    return BashPermissionSuggestion(escaped, escaped)


def _command_without_redundant_cwd(
    command: str,
    ast: BashAstResult,
    semantic: tuple[SimpleCommand, ...],
) -> str:
    if ast.is_complete and semantic and len(semantic) < len(ast.commands):
        source = command.encode("utf-8")
        return source[semantic[0].start_byte :].decode("utf-8").strip()
    return command.strip()


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
    semantic_commands = _semantic_commands(ast, command, cwd)
    if any(not _environment_is_safe(item) for item in semantic_commands):
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


def _semantic_commands(
    ast: BashAstResult, command: str, cwd: Path
) -> tuple[SimpleCommand, ...]:
    commands = ast.commands
    if len(commands) < 2:
        return commands
    first, second = commands[0], commands[1]
    if (
        first.argv[:1] != ("cd",)
        or len(first.argv) != 2
        or first.environment
        or first.redirections
    ):
        return commands
    target = Path(first.argv[1]).expanduser()
    if not target.is_absolute():
        target = cwd / target
    try:
        is_current = target.resolve(strict=False) == cwd.resolve(strict=False)
    except OSError:
        return commands
    source = command.encode("utf-8")
    connector = source[first.end_byte : second.start_byte].decode(
        "utf-8", errors="strict"
    )
    if is_current and re.fullmatch(r"\s*&&\s*", connector):
        return commands[1:]
    return commands


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
        if redirect.kind == "output" and redirect.target != "/dev/null":
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


def _escape_exact_rule(command: str) -> str:
    result: list[str] = []
    for character in command:
        if character in {"\\", "*"}:
            result.append("\\")
        result.append(character)
    return "".join(result)


__all__ = [
    "BashAnalysis",
    "BashPermissionSuggestion",
    "allowing_rules",
    "analyze_bash_command",
    "bash_rule_has_wildcard",
    "bash_rule_matches",
    "matching_rule",
    "suggest_bash_permission",
]
