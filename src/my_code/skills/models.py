"""Normalized Skill values shared by every discovery source."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

_SKILL_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


class SkillSourceKind(StrEnum):
    BUILTIN = "builtin"
    MCP = "mcp"
    USER = "user"
    PROJECT = "project"


@dataclass(frozen=True, slots=True, order=True)
class SkillSourceId:
    """Stable source identity and explicit conflict priority."""

    priority: int
    kind: SkillSourceKind
    name: str

    def __post_init__(self) -> None:
        if self.priority < 0:
            raise ValueError("Skill source priority must not be negative")
        if not self.name.strip():
            raise ValueError("Skill source name must not be blank")

    def __str__(self) -> str:
        return f"{self.kind.value}:{self.name}"


@dataclass(frozen=True, slots=True)
class SkillFingerprint:
    device: int
    inode: int
    size: int
    modified_ns: int


@dataclass(frozen=True, slots=True)
class SkillMetadata:
    name: str
    description: str
    allowed_tools: tuple[str, ...] | None = None
    compatibility: str | None = None

    def __post_init__(self) -> None:
        if _SKILL_NAME.fullmatch(self.name) is None:
            raise ValueError("Skill name must match [a-z0-9][a-z0-9_-]{0,63}")
        if not self.description.strip():
            raise ValueError("Skill description must not be blank")
        if len(self.description) > 500:
            raise ValueError("Skill description must not exceed 500 characters")
        from my_code.permissions.rules import validate_permission_rule

        allowed_tools = self.allowed_tools or ()
        for rule in allowed_tools:
            validate_permission_rule(rule)
        if len(allowed_tools) != len(set(allowed_tools)):
            raise ValueError("Skill allowed-tools must not contain duplicates")
        if self.compatibility is not None and not self.compatibility.strip():
            raise ValueError("Skill compatibility must be non-empty or omitted")


@dataclass(frozen=True, slots=True)
class SkillIndexEntry:
    """Selection metadata; filesystem instruction bodies remain unloaded."""

    metadata: SkillMetadata
    source: SkillSourceId
    locator: str
    path: Path | None = field(default=None, repr=False)
    fingerprint: SkillFingerprint | None = field(default=None, repr=False)
    inline_body: str | None = field(default=None, repr=False)

    @property
    def name(self) -> str:
        return self.metadata.name

    @property
    def description(self) -> str:
        return self.metadata.description

    @property
    def allowed_tools(self) -> tuple[str, ...] | None:
        return self.metadata.allowed_tools

    @property
    def compatibility(self) -> str | None:
        return self.metadata.compatibility


@dataclass(frozen=True, slots=True)
class SkillDefinition:
    """Fully loaded, validated Skill data ready for one activation."""

    metadata: SkillMetadata
    instructions: str
    source: SkillSourceId
    locator: str

    def __post_init__(self) -> None:
        if not self.instructions.strip():
            raise ValueError("Skill instructions must not be blank")

    @property
    def name(self) -> str:
        return self.metadata.name

    @property
    def description(self) -> str:
        return self.metadata.description

    @property
    def allowed_tools(self) -> tuple[str, ...] | None:
        return self.metadata.allowed_tools

    @property
    def compatibility(self) -> str | None:
        return self.metadata.compatibility


class SkillDiagnosticCode(StrEnum):
    IO_ERROR = "io_error"
    INVALID_FRONTMATTER = "invalid_frontmatter"
    MISSING_BODY = "missing_body"
    MISSING_FILE = "missing_file"
    PATH_ESCAPE = "path_escape"
    SAME_LAYER_CONFLICT = "same_layer_conflict"
    SYMLINK = "symlink"


@dataclass(frozen=True, slots=True)
class SkillDiagnostic:
    code: SkillDiagnosticCode
    source: SkillSourceId
    locator: str
    message: str

    def __post_init__(self) -> None:
        if not self.locator.strip() or not self.message.strip():
            raise ValueError("Skill diagnostic locator and message must not be blank")


class SkillLoadError(RuntimeError):
    """An indexed Skill can no longer be loaded safely."""


def validate_skill_name(name: str) -> str:
    SkillMetadata(name, "validation")
    return name


__all__ = [
    "SkillDefinition",
    "SkillDiagnostic",
    "SkillDiagnosticCode",
    "SkillFingerprint",
    "SkillIndexEntry",
    "SkillLoadError",
    "SkillMetadata",
    "SkillSourceId",
    "SkillSourceKind",
    "validate_skill_name",
]
