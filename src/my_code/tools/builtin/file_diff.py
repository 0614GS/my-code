"""Bounded, provider-neutral diffs for successful file writes."""

from __future__ import annotations

from difflib import SequenceMatcher

from my_code.conversation.presentation import (
    MAX_FILE_DIFF_RECORDS,
    FileDiffHunk,
    FileDiffLine,
    FileDiffPresentation,
)
from my_code.foundation.json import JsonObject

MAX_DIFF_BYTES = 2 * 1024 * 1024
MAX_DIFF_SOURCE_LINES = 50_000
MAX_DIFF_RECORDS = MAX_FILE_DIFF_RECORDS
_SELECTION_TARGET = 170


def build_file_diff(
    path: str, before: str, after: str, *, created: bool
) -> FileDiffPresentation:
    operation = "created" if created else "updated"
    old_newline = before.endswith("\n")
    new_newline = after.endswith("\n")
    if (
        len(before.encode("utf-8")) > MAX_DIFF_BYTES
        or len(after.encode("utf-8")) > MAX_DIFF_BYTES
        or _line_count(before) > MAX_DIFF_SOURCE_LINES
        or _line_count(after) > MAX_DIFF_SOURCE_LINES
    ):
        return FileDiffPresentation(
            path,
            operation,
            0,
            0,
            old_ends_with_newline=old_newline,
            new_ends_with_newline=new_newline,
            omitted_reason="diff omitted because the file is too large",
        )

    old_lines = before.splitlines(keepends=True)
    new_lines = after.splitlines(keepends=True)
    matcher = SequenceMatcher(None, old_lines, new_lines)
    opcodes = matcher.get_opcodes()
    additions = sum(
        j2 - j1 for tag, _, _, j1, j2 in opcodes if tag in {"insert", "replace"}
    )
    deletions = sum(
        i2 - i1 for tag, i1, i2, _, _ in opcodes if tag in {"delete", "replace"}
    )
    hunks: list[FileDiffHunk] = []
    for group in matcher.get_grouped_opcodes(3):
        lines: list[FileDiffLine] = []
        for tag, i1, i2, j1, j2 in group:
            if tag == "equal":
                lines.extend(
                    FileDiffLine(
                        "context",
                        _line_text(old_lines[index]),
                        index + 1,
                        j1 + offset + 1,
                    )
                    for offset, index in enumerate(range(i1, i2))
                )
            else:
                lines.extend(
                    FileDiffLine("deletion", _line_text(old_lines[index]), index + 1)
                    for index in range(i1, i2)
                )
                lines.extend(
                    FileDiffLine(
                        "addition", _line_text(new_lines[index]), new_line=index + 1
                    )
                    for index in range(j1, j2)
                )
        hunks.append(
            FileDiffHunk(
                group[0][1] + 1,
                group[-1][2] - group[0][1],
                group[0][3] + 1,
                group[-1][4] - group[0][3],
                tuple(lines),
            )
        )
    bounded, omitted = _bounded_hunks(tuple(hunks))
    return FileDiffPresentation(
        path,
        operation,
        additions,
        deletions,
        bounded,
        old_newline,
        new_newline,
        omitted,
    )


def file_diff_to_json(diff: FileDiffPresentation) -> JsonObject:
    return {
        "path": diff.path,
        "operation": diff.operation,
        "additions": diff.additions,
        "deletions": diff.deletions,
        "hunks": [
            {
                "old_start": hunk.old_start,
                "old_count": hunk.old_count,
                "new_start": hunk.new_start,
                "new_count": hunk.new_count,
                "lines": [
                    {
                        "kind": line.kind,
                        "text": line.text,
                        "old_line": line.old_line,
                        "new_line": line.new_line,
                        "omitted_lines": line.omitted_lines,
                    }
                    for line in hunk.lines
                ],
            }
            for hunk in diff.hunks
        ],
        "old_ends_with_newline": diff.old_ends_with_newline,
        "new_ends_with_newline": diff.new_ends_with_newline,
        "omitted_lines": diff.omitted_lines,
        "omitted_reason": diff.omitted_reason,
    }


def file_diff_from_json(value: object) -> FileDiffPresentation:
    if not isinstance(value, dict):
        raise TypeError("file diff metadata must be an object")
    hunks_value = value.get("hunks")
    if not isinstance(hunks_value, list):
        raise TypeError("file diff hunks must be a list")
    hunks: list[FileDiffHunk] = []
    for raw_hunk in hunks_value:
        if not isinstance(raw_hunk, dict) or not isinstance(
            raw_hunk.get("lines"), list
        ):
            raise TypeError("invalid file diff hunk metadata")
        lines = tuple(
            FileDiffLine(
                _string(raw_line, "kind"),  # type: ignore[arg-type]
                _string(raw_line, "text"),
                _optional_int(raw_line, "old_line"),
                _optional_int(raw_line, "new_line"),
                _int(raw_line, "omitted_lines"),
            )
            for raw_line in raw_hunk["lines"]
            if isinstance(raw_line, dict)
        )
        if len(lines) != len(raw_hunk["lines"]):
            raise TypeError("invalid file diff line metadata")
        hunks.append(
            FileDiffHunk(
                _int(raw_hunk, "old_start"),
                _int(raw_hunk, "old_count"),
                _int(raw_hunk, "new_start"),
                _int(raw_hunk, "new_count"),
                lines,
            )
        )
    operation = _string(value, "operation")
    return FileDiffPresentation(
        _string(value, "path"),
        operation,  # type: ignore[arg-type]
        _int(value, "additions"),
        _int(value, "deletions"),
        tuple(hunks),
        _bool(value, "old_ends_with_newline"),
        _bool(value, "new_ends_with_newline"),
        _int(value, "omitted_lines"),
        _optional_string(value, "omitted_reason"),
    )


def _bounded_hunks(
    hunks: tuple[FileDiffHunk, ...],
) -> tuple[tuple[FileDiffHunk, ...], int]:
    total = sum(len(hunk.lines) for hunk in hunks)
    if total <= MAX_DIFF_RECORDS:
        return hunks, 0

    flattened = [
        (hunk_index, line_index, line)
        for hunk_index, hunk in enumerate(hunks)
        for line_index, line in enumerate(hunk.lines)
    ]
    changed = [
        index for index, item in enumerate(flattened) if item[2].kind != "context"
    ]
    if len(changed) > _SELECTION_TARGET:
        by_hunk: dict[int, list[int]] = {}
        for index in changed:
            by_hunk.setdefault(flattened[index][0], []).append(index)
        selected = set()
        if len(by_hunk) <= _SELECTION_TARGET // 2:
            for indexes in by_hunk.values():
                selected.add(indexes[0])
                selected.add(indexes[-1])
        candidates = [index for index in changed if index not in selected]
        left = 0
        right = len(candidates) - 1
        take_left = True
        while len(selected) < _SELECTION_TARGET and left <= right:
            if take_left:
                selected.add(candidates[left])
                left += 1
            else:
                selected.add(candidates[right])
                right -= 1
            take_left = not take_left
    else:
        selected = set(changed)
        context = sorted(
            (index for index in range(len(flattened)) if index not in selected),
            key=lambda index: min(
                (abs(index - item) for item in changed), default=index
            ),
        )
        selected.update(context[: _SELECTION_TARGET - len(selected)])

    output, omitted = _project_selection(hunks, selected)
    while record_count(output) > MAX_DIFF_RECORDS:
        contexts = [
            index for index in selected if flattened[index][2].kind == "context"
        ]
        if contexts:
            selected.remove(
                max(
                    contexts,
                    key=lambda index: min(
                        (abs(index - item) for item in changed), default=index
                    ),
                )
            )
        else:
            ordered = sorted(selected)
            selected.remove(ordered[len(ordered) // 2])
        output, omitted = _project_selection(hunks, selected)
    return output, omitted


def _project_selection(
    hunks: tuple[FileDiffHunk, ...], selected: set[int]
) -> tuple[tuple[FileDiffHunk, ...], int]:
    output: list[FileDiffHunk] = []
    flat_offset = 0
    for hunk in hunks:
        kept = [
            index for index in range(len(hunk.lines)) if flat_offset + index in selected
        ]
        flat_offset += len(hunk.lines)
        if not kept:
            continue
        lines: list[FileDiffLine] = []
        previous = -1
        for index in kept:
            gap = index - previous - 1
            if gap:
                lines.append(FileDiffLine("omitted", "", omitted_lines=gap))
            lines.append(hunk.lines[index])
            previous = index
        trailing = len(hunk.lines) - previous - 1
        if trailing:
            lines.append(FileDiffLine("omitted", "", omitted_lines=trailing))
        output.append(
            FileDiffHunk(
                hunk.old_start,
                hunk.old_count,
                hunk.new_start,
                hunk.new_count,
                tuple(lines),
            )
        )
    total = sum(len(hunk.lines) for hunk in hunks)
    return tuple(output), total - len(selected)


def record_count(hunks: tuple[FileDiffHunk, ...]) -> int:
    return sum(len(hunk.lines) for hunk in hunks)


def _line_count(value: str) -> int:
    return len(value.splitlines())


def _line_text(value: str) -> str:
    return value.removesuffix("\n").removesuffix("\r")


def _string(value: dict[object, object], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str):
        raise TypeError(f"file diff {key} must be a string")
    return item


def _int(value: dict[object, object], key: str) -> int:
    item = value.get(key)
    if not isinstance(item, int) or isinstance(item, bool):
        raise TypeError(f"file diff {key} must be an integer")
    return item


def _optional_int(value: dict[object, object], key: str) -> int | None:
    item = value.get(key)
    if item is not None and (not isinstance(item, int) or isinstance(item, bool)):
        raise TypeError(f"file diff {key} must be an integer or null")
    return item


def _bool(value: dict[object, object], key: str) -> bool:
    item = value.get(key)
    if not isinstance(item, bool):
        raise TypeError(f"file diff {key} must be a boolean")
    return item


def _optional_string(value: dict[object, object], key: str) -> str | None:
    item = value.get(key)
    if item is not None and not isinstance(item, str):
        raise TypeError(f"file diff {key} must be a string or null")
    return item


__all__ = [
    "MAX_DIFF_BYTES",
    "MAX_DIFF_RECORDS",
    "MAX_DIFF_SOURCE_LINES",
    "build_file_diff",
    "file_diff_from_json",
    "file_diff_to_json",
]
