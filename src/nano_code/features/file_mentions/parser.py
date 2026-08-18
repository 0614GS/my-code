"""Parse file mentions from submitted user prompts."""

from nano_code.features.file_mentions.models import FileMention


def parse_file_mentions(prompt: str) -> tuple[FileMention, ...]:
    """Parse valid mentions in first-seen order, ignoring email-like ``@`` text."""

    mentions: list[FileMention] = []
    seen: set[tuple[str, int | None, int | None]] = set()
    index = 0
    while index < len(prompt):
        at = prompt.find("@", index)
        if at < 0:
            break
        if at > 0 and not (prompt[at - 1].isspace() or prompt[at - 1] in "([{,:"):
            index = at + 1
            continue
        cursor = at + 1
        if cursor >= len(prompt):
            break
        if prompt[cursor] == '"':
            close = prompt.find('"', cursor + 1)
            if close < 0:
                index = cursor + 1
                continue
            path = prompt[cursor + 1 : close]
            cursor = close + 1
        else:
            begin = cursor
            while cursor < len(prompt) and not prompt[cursor].isspace():
                cursor += 1
            path = prompt[begin:cursor]

        line_start: int | None = None
        line_end: int | None = None
        range_at = path.rfind("#L")
        if range_at >= 0:
            parsed = _parse_line_range(path[range_at + 2 :])
            if parsed is None:
                index = cursor
                continue
            path = path[:range_at]
            line_start, line_end = parsed
        elif cursor < len(prompt) and prompt.startswith("#L", cursor):
            range_end = cursor + 2
            while range_end < len(prompt) and (
                prompt[range_end].isdigit() or prompt[range_end] == "-"
            ):
                range_end += 1
            parsed = _parse_line_range(prompt[cursor + 2 : range_end])
            if parsed is None:
                index = range_end
                continue
            line_start, line_end = parsed
            cursor = range_end

        if not path or path.startswith("@"):
            index = cursor
            continue
        key = (path, line_start, line_end)
        if key not in seen:
            seen.add(key)
            mentions.append(
                FileMention(
                    path=path,
                    raw=prompt[at:cursor],
                    start=at,
                    end=cursor,
                    line_start=line_start,
                    line_end=line_end,
                )
            )
        index = cursor
    return tuple(mentions)


def _parse_line_range(value: str) -> tuple[int, int] | None:
    parts = value.split("-", 1)
    if not parts[0].isdigit():
        return None
    start = int(parts[0])
    end = start
    if len(parts) == 2:
        if not parts[1].isdigit():
            return None
        end = int(parts[1])
    if start < 1 or end < start or end - start + 1 > 5000:
        return None
    return start, end
