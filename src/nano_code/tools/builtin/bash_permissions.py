"""Conservative, input-aware permission analysis for the Bash tool."""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from pathlib import Path

_SHELL_OPERATORS = frozenset({"|", "&&", "||", ";"})
_REDIRECTION_OPERATORS = frozenset({"<", ">", "<<", ">>", "<&", ">&", "<<<"})
_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*", re.DOTALL)


@dataclass(frozen=True, slots=True)
class BashAnalysis:
    """The stable facts used by Bash permissions, scheduling, and tests."""

    is_read_only: bool
    reason: str
    commands: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _OptionSpec:
    flags: frozenset[str]
    value_flags: frozenset[str] = frozenset()
    allow_short_clusters: bool = True


_PATH_COMMANDS: dict[str, _OptionSpec] = {
    "ls": _OptionSpec(
        frozenset(
            {
                "-a",
                "-A",
                "-l",
                "-h",
                "-d",
                "-F",
                "-R",
                "-r",
                "-S",
                "-t",
                "-1",
                "--all",
                "--almost-all",
                "--human-readable",
                "--directory",
                "--classify",
                "--recursive",
                "--reverse",
                "--size",
                "--time",
            }
        )
    ),
    "cat": _OptionSpec(
        frozenset(
            {
                "-A",
                "-b",
                "-E",
                "-n",
                "-s",
                "-T",
                "-v",
                "--show-all",
                "--number-nonblank",
                "--show-ends",
                "--number",
                "--squeeze-blank",
                "--show-tabs",
                "--show-nonprinting",
            }
        )
    ),
    "head": _OptionSpec(
        frozenset({"-q", "-v", "--quiet", "--verbose"}),
        frozenset({"-n", "--lines", "-c", "--bytes"}),
    ),
    "tail": _OptionSpec(
        frozenset({"-q", "-v", "--quiet", "--verbose"}),
        frozenset({"-n", "--lines", "-c", "--bytes"}),
    ),
    "wc": _OptionSpec(
        frozenset(
            {
                "-c",
                "-m",
                "-l",
                "-L",
                "-w",
                "--bytes",
                "--chars",
                "--lines",
                "--max-line-length",
                "--words",
            }
        )
    ),
    "stat": _OptionSpec(
        frozenset({"-L", "-f", "-t", "--dereference", "--file-system", "--terse"}),
        frozenset({"-c", "--format", "--printf"}),
    ),
    "file": _OptionSpec(
        frozenset(
            {
                "-b",
                "-i",
                "-L",
                "-h",
                "-k",
                "-s",
                "--brief",
                "--mime",
                "--dereference",
                "--no-dereference",
                "--keep-going",
                "--special-files",
            }
        )
    ),
}

_RG_OPTIONS = _OptionSpec(
    frozenset(
        {
            "-F",
            "-i",
            "-s",
            "-S",
            "-v",
            "-w",
            "-x",
            "-l",
            "-L",
            "-c",
            "-n",
            "-N",
            "-H",
            "-h",
            "-0",
            "--fixed-strings",
            "--ignore-case",
            "--case-sensitive",
            "--smart-case",
            "--invert-match",
            "--word-regexp",
            "--line-regexp",
            "--files-with-matches",
            "--files-without-match",
            "--count",
            "--line-number",
            "--no-line-number",
            "--with-filename",
            "--no-filename",
            "--hidden",
            "--no-ignore",
            "--no-messages",
            "--files",
        }
    ),
    frozenset(
        {
            "-g",
            "--glob",
            "-t",
            "--type",
            "-T",
            "--type-not",
            "-m",
            "--max-count",
            "--max-depth",
            "-A",
            "--after-context",
            "-B",
            "--before-context",
            "-C",
            "--context",
        }
    ),
)

_GREP_OPTIONS = _OptionSpec(
    frozenset(
        {
            "-E",
            "-F",
            "-G",
            "-P",
            "-i",
            "-v",
            "-w",
            "-x",
            "-n",
            "-H",
            "-h",
            "-l",
            "-L",
            "-c",
            "-r",
            "-R",
            "-s",
            "--extended-regexp",
            "--fixed-strings",
            "--basic-regexp",
            "--perl-regexp",
            "--ignore-case",
            "--invert-match",
            "--word-regexp",
            "--line-regexp",
            "--line-number",
            "--with-filename",
            "--no-filename",
            "--files-with-matches",
            "--files-without-match",
            "--count",
            "--recursive",
            "--dereference-recursive",
            "--no-messages",
        }
    ),
    frozenset(
        {
            "-m",
            "--max-count",
            "-A",
            "--after-context",
            "-B",
            "--before-context",
            "-C",
            "--context",
            "--include",
            "--exclude",
            "--exclude-dir",
        }
    ),
)

_GIT_OPTIONS: dict[str, _OptionSpec] = {
    "status": _OptionSpec(
        frozenset(
            {
                "-s",
                "-b",
                "--short",
                "--branch",
                "--porcelain",
                "--long",
                "--verbose",
                "-v",
                "--untracked-files",
                "--ignored",
                "--no-renames",
            }
        )
    ),
    "diff": _OptionSpec(
        frozenset(
            {
                "-p",
                "--patch",
                "--stat",
                "--shortstat",
                "--numstat",
                "--name-only",
                "--name-status",
                "--summary",
                "--cached",
                "--staged",
                "--check",
                "--quiet",
                "--exit-code",
                "--no-ext-diff",
                "--no-renames",
                "--color",
                "--no-color",
            }
        ),
        frozenset({"-U", "--unified", "--diff-filter"}),
        allow_short_clusters=False,
    ),
    "log": _OptionSpec(
        frozenset(
            {
                "--oneline",
                "--stat",
                "--shortstat",
                "--name-only",
                "--name-status",
                "--decorate",
                "--graph",
                "--all",
                "--reverse",
                "--no-merges",
                "--merges",
                "--first-parent",
                "--color",
                "--no-color",
            }
        ),
        frozenset({"-n", "--max-count", "--since", "--until", "--author"}),
        allow_short_clusters=False,
    ),
    "show": _OptionSpec(
        frozenset(
            {
                "--oneline",
                "--stat",
                "--name-only",
                "--name-status",
                "--summary",
                "--color",
                "--no-color",
            }
        ),
        frozenset({"--format"}),
        allow_short_clusters=False,
    ),
    "blame": _OptionSpec(
        frozenset({"-w", "--show-name", "--show-number", "--porcelain"}),
        frozenset({"-L"}),
        allow_short_clusters=False,
    ),
    "ls-files": _OptionSpec(
        frozenset({"-c", "-d", "-m", "-o", "-i", "-s", "-u", "--cached"})
    ),
    "rev-parse": _OptionSpec(
        frozenset(
            {
                "--verify",
                "--short",
                "--show-toplevel",
                "--show-prefix",
                "--is-inside-work-tree",
                "--abbrev-ref",
                "--symbolic-full-name",
            }
        )
    ),
    "branch": _OptionSpec(
        frozenset({"-a", "-r", "-v", "--all", "--remotes", "--list", "--show-current"})
    ),
    "tag": _OptionSpec(frozenset({"-l", "-n", "--list"})),
}


def analyze_bash_command(command: str, cwd: Path) -> BashAnalysis:
    """Prove a small subset of shell commands read-only or fail closed."""

    lexical_error = _find_unsafe_shell_syntax(command)
    if lexical_error is not None:
        return BashAnalysis(False, lexical_error)

    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars="|&;<>")
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError as error:
        return BashAnalysis(False, f"shell syntax cannot be parsed: {error}")

    commands = _split_commands(tokens)
    if commands is None:
        return BashAnalysis(False, "shell operators or redirections are not supported")
    normalized = tuple(" ".join(parts) for parts in commands)
    if not commands:
        return BashAnalysis(False, "empty shell command")

    for parts in commands:
        safe, reason = _command_is_read_only(parts, cwd)
        if not safe:
            return BashAnalysis(False, reason, normalized)
    return BashAnalysis(True, "every shell subcommand is read-only", normalized)


def bash_rule_matches(rule_content: str, command: str) -> bool:
    """Match exact or Claude-compatible ``:*`` command-prefix rules."""

    normalized_rule = rule_content.strip()
    normalized_command = command.strip()
    if normalized_rule.endswith(":*"):
        prefix = normalized_rule[:-2].rstrip()
        return normalized_command == prefix or normalized_command.startswith(
            prefix + " "
        )
    if normalized_rule.endswith(" *"):
        prefix = normalized_rule[:-2].rstrip()
        return normalized_command == prefix or normalized_command.startswith(
            prefix + " "
        )
    return normalized_command == normalized_rule


def _find_unsafe_shell_syntax(command: str) -> str | None:
    if "\n" in command or "\r" in command:
        return "multi-line shell input requires approval"

    single_quoted = False
    double_quoted = False
    escaped = False
    for character in command:
        if escaped:
            escaped = False
            continue
        if character == "\\" and not single_quoted:
            escaped = True
            continue
        if character == "'" and not double_quoted:
            single_quoted = not single_quoted
            continue
        if character == '"' and not single_quoted:
            double_quoted = not double_quoted
            continue
        if single_quoted:
            continue
        if character in {"$", "`"}:
            return "shell expansion or command substitution requires approval"
        if not double_quoted and character in {"*", "?", "[", "]"}:
            return "unquoted glob expansion requires approval"
        if not double_quoted and character in {"(", ")", "{", "}", "#"}:
            return "complex shell syntax requires approval"
    if escaped or single_quoted or double_quoted:
        return "unterminated shell quoting requires approval"
    return None


def _split_commands(tokens: list[str]) -> list[list[str]] | None:
    commands: list[list[str]] = []
    current: list[str] = []
    for token in tokens:
        if token in _REDIRECTION_OPERATORS or set(token) <= {"<", ">"}:
            return None
        if token == "&":
            return None
        if token in _SHELL_OPERATORS:
            if not current:
                return None
            commands.append(current)
            current = []
            continue
        current.append(token)
    if not current:
        return None
    commands.append(current)
    return commands


def _command_is_read_only(parts: list[str], cwd: Path) -> tuple[bool, str]:
    name, *arguments = parts
    if _ASSIGNMENT.fullmatch(name):
        return False, "environment-prefixed commands require approval"
    if name in {"pwd", "whoami", "uptime", "true", "false"}:
        return (not arguments, f"{name} only allows an argument-free invocation")
    if name in {"python", "python3"}:
        return (
            arguments == ["--version"],
            f"only the exact {name} version query is read-only",
        )
    if name == "node":
        return (
            arguments in (["--version"], ["-v"]),
            "only the exact node version query is read-only",
        )
    if name in _PATH_COMMANDS:
        return _validate_path_command(name, arguments, cwd)
    if name in {"rg", "grep"}:
        return _validate_search_command(name, arguments, cwd)
    if name == "find":
        return _validate_find(arguments, cwd)
    if name == "git":
        return _validate_git(arguments)
    return False, f"{name!r} is not in the read-only command allowlist"


def _validate_path_command(
    name: str, arguments: list[str], cwd: Path
) -> tuple[bool, str]:
    positionals = _parse_options(arguments, _PATH_COMMANDS[name])
    if positionals is None:
        return False, f"{name} contains an unsupported option"
    if all(_is_workspace_path(value, cwd) for value in positionals):
        return True, f"{name} only reads workspace paths"
    return False, f"{name} references a path outside the workspace"


def _validate_search_command(
    name: str, arguments: list[str], cwd: Path
) -> tuple[bool, str]:
    spec = _RG_OPTIONS if name == "rg" else _GREP_OPTIONS
    positionals = _parse_options(arguments, spec)
    if positionals is None:
        return False, f"{name} contains an unsupported option"
    if "--files" in arguments and name == "rg":
        paths = positionals
    else:
        if not positionals:
            return False, f"{name} requires an explicit search pattern"
        paths = positionals[1:]
    if all(_is_workspace_path(value, cwd) for value in paths):
        return True, f"{name} searches workspace paths"
    return False, f"{name} references a path outside the workspace"


def _validate_find(arguments: list[str], cwd: Path) -> tuple[bool, str]:
    if not arguments:
        return True, "find reads the current workspace"
    paths: list[str] = []
    index = 0
    while index < len(arguments) and not arguments[index].startswith("-"):
        paths.append(arguments[index])
        index += 1
    if not paths:
        paths = ["."]
    if not all(_is_workspace_path(value, cwd) for value in paths):
        return False, "find references a path outside the workspace"

    value_predicates = {
        "-name",
        "-iname",
        "-path",
        "-ipath",
        "-type",
        "-maxdepth",
        "-mindepth",
    }
    no_value_predicates = {"-print", "-print0", "-empty"}
    while index < len(arguments):
        predicate = arguments[index]
        if predicate in no_value_predicates:
            index += 1
        elif predicate in value_predicates and index + 1 < len(arguments):
            index += 2
        else:
            return False, f"find predicate {predicate!r} is not read-only"
    return True, "find uses read-only predicates"


def _validate_git(arguments: list[str]) -> tuple[bool, str]:
    if not arguments:
        return False, "git requires a read-only subcommand"
    if arguments[0].startswith("-"):
        return False, "git global options require approval"
    subcommand, *subcommand_arguments = arguments
    spec = _GIT_OPTIONS.get(subcommand)
    if spec is None:
        return False, f"git {subcommand} is not a recognized read-only operation"
    positionals = _parse_options(subcommand_arguments, spec)
    if positionals is None:
        return False, f"git {subcommand} contains an unsupported option"
    if subcommand in {"branch", "tag"} and positionals:
        return False, f"git {subcommand} with positional arguments may write"
    return True, f"git {subcommand} is read-only with these options"


def _parse_options(arguments: list[str], spec: _OptionSpec) -> list[str] | None:
    positionals: list[str] = []
    index = 0
    options_finished = False
    while index < len(arguments):
        argument = arguments[index]
        if options_finished or argument == "-" or not argument.startswith("-"):
            positionals.append(argument)
            index += 1
            continue
        if argument == "--":
            options_finished = True
            index += 1
            continue

        name, separator, _value = argument.partition("=")
        if name in spec.value_flags:
            if separator:
                index += 1
                continue
            if index + 1 >= len(arguments):
                return None
            index += 2
            continue
        if argument in spec.flags:
            index += 1
            continue
        if (
            spec.allow_short_clusters
            and argument.startswith("-")
            and not argument.startswith("--")
            and len(argument) > 2
            and all(f"-{character}" in spec.flags for character in argument[1:])
        ):
            index += 1
            continue
        return None
    return positionals


def _is_workspace_path(value: str, cwd: Path) -> bool:
    if value == "-":
        return True
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = cwd / candidate
    try:
        candidate.resolve(strict=False).relative_to(cwd.resolve(strict=False))
    except ValueError:
        return False
    return True
