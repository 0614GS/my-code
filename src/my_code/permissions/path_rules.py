"""Permission-owned matching for file rule content."""

import re

from my_code.permissions.models import (
    PermissionBehavior,
    PermissionRule,
)


def matching_path_rule(pattern: str | None, path: str) -> bool:
    """Match file permission wildcard syntax against a normalized relative path."""

    if pattern is None:
        return True
    candidates = (path, f"./{path}") if path != "." else (".", "./")
    return any(_wildcard_matches(pattern, candidate) for candidate in candidates)


def read_denied(rules: tuple[PermissionRule, ...], tool_name: str, path: str) -> bool:
    """Return whether a whole-tool or path deny blocks a normalized read path."""

    return any(
        rule.tool_name == tool_name
        and rule.behavior is PermissionBehavior.DENY
        and (rule.applies_to_entire_tool or matching_path_rule(rule.rule_content, path))
        for rule in rules
    )


def _wildcard_matches(pattern: str, value: str) -> bool:
    pattern = pattern.strip().replace("\\/", "/")
    parts: list[str] = []
    index = 0
    has_wildcard = False
    while index < len(pattern):
        if pattern[index] == "\\" and index + 1 < len(pattern):
            following = pattern[index + 1]
            if following in {"*", "\\"}:
                parts.append(re.escape(following))
                index += 2
                continue
        if pattern[index] == "*":
            parts.append(".*")
            has_wildcard = True
        else:
            parts.append(re.escape(pattern[index]))
        index += 1
    regex = "".join(parts)
    literal = pattern.replace(r"\*", "*").replace(r"\\", "\\")
    return (
        re.fullmatch(regex, value, flags=re.DOTALL) is not None
        if has_wildcard
        else value == literal
    )


__all__ = [
    "matching_path_rule",
    "read_denied",
]
