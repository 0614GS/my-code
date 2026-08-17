"""Fail-closed Bash AST parsing for permission facts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import tree_sitter_bash
from tree_sitter import Language, Node, Parser

MAX_ANALYSIS_CHARACTERS = 10_000
MAX_AST_NODES = 50_000

_LANGUAGE = Language(tree_sitter_bash.language())
_STATIC_WORD_NODES = frozenset(
    {"word", "raw_string", "string", "concatenation", "number", "command_name"}
)
_SUPPORTED_NAMED_NODES = frozenset(
    {
        "program",
        "list",
        "pipeline",
        "redirected_statement",
        "command",
        "command_name",
        "variable_assignment",
        "variable_name",
        "file_redirect",
        "file_descriptor",
        "word",
        "raw_string",
        "string",
        "string_content",
        "concatenation",
        "number",
        "comment",
    }
)
_OUTPUT_OPERATORS = frozenset({">", ">>", ">|", "&>", "&>>"})
_INPUT_OPERATORS = frozenset({"<"})
_FD_OPERATORS = frozenset({"<&", ">&"})


@dataclass(frozen=True, slots=True)
class EnvironmentAssignment:
    name: str
    value: str


@dataclass(frozen=True, slots=True)
class Redirection:
    source: str
    operator: str
    target: str
    descriptor: int | None
    kind: Literal["input", "output", "fd"]


@dataclass(frozen=True, slots=True)
class SimpleCommand:
    source: str
    rule_source: str
    start_byte: int
    end_byte: int
    argv: tuple[str, ...]
    environment: tuple[EnvironmentAssignment, ...]
    redirections: tuple[Redirection, ...]


@dataclass(frozen=True, slots=True)
class BashAstResult:
    """A complete result may authorize; too-complex facts are deny/ask-only."""

    kind: Literal["simple", "too-complex"]
    reason: str
    commands: tuple[SimpleCommand, ...] = ()
    command_sources: tuple[str, ...] = ()
    redirections: tuple[Redirection, ...] = ()

    @property
    def is_complete(self) -> bool:
        return self.kind == "simple"


def parse_bash(command: str) -> BashAstResult:
    """Parse a conservative Bash subset without a lexical fallback."""

    if not command.strip():
        return BashAstResult("too-complex", "empty shell command")
    if len(command) > MAX_ANALYSIS_CHARACTERS:
        return BashAstResult(
            "too-complex",
            f"command exceeds the {MAX_ANALYSIS_CHARACTERS:,}-character AST limit",
        )

    source = command.encode("utf-8")
    try:
        tree = Parser(_LANGUAGE).parse(source)
        nodes = _walk_with_budget(tree.root_node)
    except Exception as error:  # pragma: no cover - native parser failures are rare
        return BashAstResult("too-complex", f"Bash AST parsing failed: {error}")

    sources = _reliable_command_sources(nodes, source)
    if nodes is None:
        return BashAstResult(
            "too-complex",
            f"Bash AST exceeds the {MAX_AST_NODES:,}-node limit",
            command_sources=sources,
        )
    if any(node.is_error or node.is_missing or node.type == "ERROR" for node in nodes):
        return BashAstResult(
            "too-complex",
            "Bash syntax contains an error or missing node",
            command_sources=sources,
        )
    unsupported = next(
        (
            node.type
            for node in nodes
            if node.is_named and node.type not in _SUPPORTED_NAMED_NODES
        ),
        None,
    )
    if unsupported is None and any(node.type == "&" for node in nodes):
        unsupported = "background execution"
    if unsupported is not None:
        return BashAstResult(
            "too-complex",
            f"unsupported Bash syntax: {unsupported}",
            command_sources=sources,
        )

    redirects: list[Redirection] = []
    for node in nodes:
        if node.type == "file_redirect":
            redirect = _parse_redirect(node, source)
            if redirect is None:
                return BashAstResult(
                    "too-complex",
                    "dynamic or unsupported redirection requires approval",
                    command_sources=sources,
                )
            redirects.append(redirect)

    commands: list[SimpleCommand] = []
    for node in nodes:
        if node.type != "command":
            continue
        parsed = _parse_command(node, source)
        if parsed is None:
            return BashAstResult(
                "too-complex",
                "dynamic command, argument, or environment value requires approval",
                command_sources=sources,
                redirections=tuple(redirects),
            )
        commands.append(parsed)
    if not commands:
        return BashAstResult(
            "too-complex", "no executable command was found", command_sources=sources
        )
    return BashAstResult(
        "simple",
        "Bash AST is fully static",
        tuple(commands),
        sources,
        tuple(redirects),
    )


def _walk_with_budget(root: Node) -> list[Node] | None:
    nodes: list[Node] = []
    stack = [root]
    while stack:
        node = stack.pop()
        nodes.append(node)
        if len(nodes) > MAX_AST_NODES:
            return None
        stack.extend(reversed(node.children))
    return nodes


def _reliable_command_sources(
    nodes: list[Node] | None, source: bytes
) -> tuple[str, ...]:
    if nodes is None:
        return ()
    return tuple(
        _text(node, source)
        for node in nodes
        if node.type == "command" and node.end_byte > node.start_byte
    )


def _parse_command(node: Node, source: bytes) -> SimpleCommand | None:
    argv: list[str] = []
    environment: list[EnvironmentAssignment] = []
    redirects: list[Redirection] = []
    word_nodes: list[Node] = []
    for child in node.named_children:
        if child.type == "variable_assignment":
            assignment = _parse_assignment(child, source)
            if assignment is None:
                return None
            environment.append(assignment)
        elif child.type in _STATIC_WORD_NODES:
            value = _static_word(child, source)
            if value is None:
                return None
            argv.append(value)
            word_nodes.append(child)
        elif child.type == "file_redirect":
            redirect = _parse_redirect(child, source)
            if redirect is None:
                return None
            redirects.append(redirect)
        else:
            return None
    if not argv:
        return None
    ancestor = node.parent
    while ancestor is not None and ancestor.type != "program":
        if ancestor.type == "redirected_statement":
            for child in ancestor.named_children:
                if child.type != "file_redirect":
                    continue
                redirect = _parse_redirect(child, source)
                if redirect is None:
                    return None
                if redirect not in redirects:
                    redirects.append(redirect)
        ancestor = ancestor.parent
    return SimpleCommand(
        _text(node, source),
        source[word_nodes[0].start_byte : word_nodes[-1].end_byte].decode("utf-8"),
        node.start_byte,
        node.end_byte,
        tuple(argv),
        tuple(environment),
        tuple(redirects),
    )


def _parse_assignment(node: Node, source: bytes) -> EnvironmentAssignment | None:
    name_node = node.child_by_field_name("name")
    value_node = node.child_by_field_name("value")
    if name_node is None:
        return None
    value = "" if value_node is None else _static_word(value_node, source)
    if value is None:
        return None
    return EnvironmentAssignment(_text(name_node, source), value)


def _parse_redirect(node: Node, source: bytes) -> Redirection | None:
    operator = next(
        (
            child.type
            for child in node.children
            if not child.is_named
            and child.type in _OUTPUT_OPERATORS | _INPUT_OPERATORS | _FD_OPERATORS
        ),
        None,
    )
    if operator is None:
        return None
    descriptor_node = node.child_by_field_name("descriptor")
    descriptor = (
        int(_text(descriptor_node, source)) if descriptor_node is not None else None
    )
    destination = node.child_by_field_name("destination")
    if destination is None:
        return None
    target = _static_word(destination, source)
    if target is None:
        return None
    if operator in _INPUT_OPERATORS:
        kind: Literal["input", "output", "fd"] = "input"
    elif operator in _OUTPUT_OPERATORS:
        kind = "output"
    elif destination.type in {"number", "file_descriptor"} or target == "-":
        kind = "fd"
    else:
        kind = "input" if operator == "<&" else "output"
    return Redirection(_text(node, source), operator, target, descriptor, kind)


def _static_word(node: Node, source: bytes) -> str | None:
    if node.type == "command_name":
        return (
            _static_word(node.named_children[0], source)
            if node.named_children
            else None
        )
    if node.type in {"number", "file_descriptor"}:
        return _text(node, source)
    if node.type == "raw_string":
        text = _text(node, source)
        return text[1:-1]
    if node.type == "string":
        if any(child.type not in {"string_content"} for child in node.named_children):
            return None
        return _decode_double_quoted(_text(node, source)[1:-1])
    if node.type == "concatenation":
        values = [_static_word(child, source) for child in node.named_children]
        if any(value is None for value in values):
            return None
        return "".join(value for value in values if value is not None)
    if node.type != "word":
        return None
    text = _text(node, source)
    if _has_unquoted_expansion(text):
        return None
    return _decode_unquoted(text)


def _has_unquoted_expansion(text: str) -> bool:
    escaped = False
    for character in text:
        if escaped:
            escaped = False
            continue
        if character == "\\":
            escaped = True
        elif character in {"*", "?", "[", "]", "{", "}"}:
            return True
    return escaped


def _decode_unquoted(text: str) -> str:
    result: list[str] = []
    index = 0
    while index < len(text):
        if text[index] == "\\" and index + 1 < len(text):
            if text[index + 1] != "\n":
                result.append(text[index + 1])
            index += 2
        else:
            result.append(text[index])
            index += 1
    return "".join(result)


def _decode_double_quoted(text: str) -> str:
    result: list[str] = []
    index = 0
    while index < len(text):
        if text[index] == "\\" and index + 1 < len(text):
            following = text[index + 1]
            if following in {"$", "`", '"', "\\"}:
                result.append(following)
                index += 2
                continue
            if following == "\n":
                index += 2
                continue
        result.append(text[index])
        index += 1
    return "".join(result)


def _text(node: Node, source: bytes) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8")
