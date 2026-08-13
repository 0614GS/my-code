"""Compact, credential-safe presentation helpers for built-in tool events."""

import json

from nano_code.messages import JsonObject, JsonValue

_MAX_SUMMARY_CHARS = 140


def tool_call_summary(name: str, tool_input: JsonObject) -> str:
    """Prefer the identifying argument over dumping a potentially huge payload."""

    preferred_keys = {
        "Bash": ("command",),
        "Read": ("path",),
        "Write": ("path",),
        "Edit": ("path",),
        "Glob": ("pattern", "path"),
        "Grep": ("pattern", "path"),
    }
    parts: list[str] = []
    for key in preferred_keys.get(name, ()):
        value = tool_input.get(key)
        if value is not None:
            parts.append(_compact_value(value))
    if not parts:
        serialized = json.dumps(tool_input, ensure_ascii=False, separators=(",", ":"))
        return _truncate(serialized)
    return _truncate(" · ".join(parts))


def tool_result_summary(name: str, content: str, *, is_error: bool) -> str:
    """Describe result size and keep at most one useful output line on screen."""

    lines = [line.strip() for line in content.splitlines() if line.strip()]
    if not lines:
        return "failed with no details" if is_error else "completed with no output"
    if name in {"Glob", "Grep"} and not is_error:
        if lines == ["<no matches>"]:
            return "no matches"
        return f"{len(lines)} match(es) · {_truncate(lines[0])}"
    if name == "Read" and not is_error:
        numbered_lines = sum(1 for line in lines[1:] if "\t" in line)
        return f"{numbered_lines} line(s) · {_truncate(lines[0])}"
    if name == "Bash" and not is_error:
        exit_line = lines[0]
        preview = lines[1] if len(lines) > 1 else "no output"
        return _truncate(f"{exit_line} · {preview}")
    return _truncate(lines[0])


def _compact_value(value: JsonValue) -> str:
    if isinstance(value, str):
        return " ".join(value.split())
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _truncate(value: str) -> str:
    if len(value) <= _MAX_SUMMARY_CHARS:
        return value
    return value[: _MAX_SUMMARY_CHARS - 1] + "…"
