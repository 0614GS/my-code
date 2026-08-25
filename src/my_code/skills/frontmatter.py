"""Small, strict SKILL.md frontmatter parser with no executable semantics."""

from __future__ import annotations

import ast
from collections.abc import Iterable

from my_code.skills.models import SkillMetadata

_KNOWN_FIELDS = {"name", "description", "allowed-tools", "compatibility"}
_MAX_FRONTMATTER_CHARS = 16 * 1024


class SkillDocumentError(ValueError):
    pass


def parse_frontmatter(lines: Iterable[str], *, default_name: str) -> SkillMetadata:
    """Parse the deliberately flat frontmatter subset used by first-party Skills."""

    values: dict[str, str] = {}
    total = 0
    for raw_line in lines:
        total += len(raw_line)
        if total > _MAX_FRONTMATTER_CHARS:
            raise SkillDocumentError("Skill frontmatter is too large")
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if raw_line[:1].isspace() or ":" not in raw_line:
            raise SkillDocumentError(
                "Skill frontmatter must use flat key: value fields"
            )
        raw_key, raw_value = raw_line.split(":", 1)
        key = raw_key.strip()
        value = raw_value.strip()
        if key not in _KNOWN_FIELDS:
            raise SkillDocumentError(f"Unknown Skill frontmatter field: {key}")
        if key in values:
            raise SkillDocumentError(f"Duplicate Skill frontmatter field: {key}")
        if not value:
            raise SkillDocumentError(f"Skill frontmatter field {key} is empty")
        values[key] = value

    description = _scalar(values.get("description", ""))
    if not description:
        raise SkillDocumentError("Skill frontmatter requires description")
    try:
        return SkillMetadata(
            name=_scalar(values.get("name", default_name)),
            description=description,
            allowed_tools=_tool_list(values.get("allowed-tools")),
            compatibility=(
                _scalar(values["compatibility"]) if "compatibility" in values else None
            ),
        )
    except ValueError as error:
        raise SkillDocumentError(str(error)) from error


def split_document(content: str, *, default_name: str) -> tuple[SkillMetadata, str]:
    lines = content.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        raise SkillDocumentError("SKILL.md must start with --- frontmatter")
    closing = next(
        (
            index
            for index, line in enumerate(lines[1:], start=1)
            if line.strip() == "---"
        ),
        None,
    )
    if closing is None:
        raise SkillDocumentError("SKILL.md frontmatter is not terminated")
    metadata = parse_frontmatter(lines[1:closing], default_name=default_name)
    body = "".join(lines[closing + 1 :]).strip()
    if not body:
        raise SkillDocumentError("Skill instruction body is missing")
    return metadata, body


def _scalar(value: str) -> str:
    if len(value) >= 2 and value[0] in {"'", '"'} and value[-1] == value[0]:
        try:
            parsed = ast.literal_eval(value)
        except (SyntaxError, ValueError) as error:
            raise SkillDocumentError("Invalid quoted frontmatter value") from error
        if not isinstance(parsed, str):
            raise SkillDocumentError("Frontmatter scalar must be a string")
        return parsed.strip()
    return value.strip()


def _tool_list(value: str | None) -> tuple[str, ...] | None:
    if value is None:
        return None
    actual = value.strip()
    if not (actual.startswith("[") and actual.endswith("]")):
        raise SkillDocumentError("allowed-tools must use [ToolA, ToolB] syntax")
    inner = actual[1:-1].strip()
    if not inner:
        return ()
    tools = tuple(_scalar(item.strip()) for item in inner.split(","))
    if any(not item for item in tools):
        raise SkillDocumentError("allowed-tools contains an empty item")
    if len(tools) != len(set(tools)):
        raise SkillDocumentError("allowed-tools must not contain duplicates")
    return tools


__all__ = [
    "SkillDocumentError",
    "parse_frontmatter",
    "split_document",
]
