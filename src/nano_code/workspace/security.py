"""Canonical workspace paths and shared path-rule matching."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from nano_code.permissions.models import (
    PermissionBehavior,
    PermissionRule,
)


class WorkspaceBoundaryError(ValueError):
    """A requested path is missing or escapes the configured workspace."""


@dataclass(frozen=True, slots=True)
class WorkspaceSecurity:
    """Resolve paths once and enforce workspace/symlink boundaries."""

    root: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", self.root.resolve())

    def resolve(self, raw_path: str, *, must_exist: bool = False) -> Path:
        candidate = Path(raw_path).expanduser()
        if not candidate.is_absolute():
            candidate = self.root / candidate
        resolved = candidate.resolve(strict=False)
        if not resolved.is_relative_to(self.root):
            raise WorkspaceBoundaryError(f"Path escapes the workspace: {raw_path}")
        if must_exist and not resolved.exists():
            raise WorkspaceBoundaryError(f"Path does not exist: {raw_path}")
        return resolved

    def display(self, path: Path) -> str:
        resolved = path.resolve(strict=False)
        if not resolved.is_relative_to(self.root):
            raise WorkspaceBoundaryError(f"Path escapes the workspace: {path}")
        return resolved.relative_to(self.root).as_posix()

    def read_denied(
        self,
        rules: tuple[PermissionRule, ...],
        tool_name: str,
        path: Path,
    ) -> bool:
        """Return whether a whole-tool or path deny blocks this read."""

        rule_path = self.display(path) or "."
        return any(
            rule.tool_name == tool_name
            and rule.behavior is PermissionBehavior.DENY
            and (
                rule.applies_to_entire_tool
                or matching_path_rule(rule.rule_content, rule_path)
            )
            for rule in rules
        )


def matching_path_rule(pattern: str | None, path: str) -> bool:
    """Match the file-tool wildcard syntax against a normalized relative path."""

    if pattern is None:
        return True
    candidates = (path, f"./{path}") if path != "." else (".", "./")
    return any(_wildcard_matches(pattern, candidate) for candidate in candidates)


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
