"""Editor behavior for path mentions in the TUI input."""


def mention_at_cursor(value: str, cursor: int) -> tuple[int, int, str] | None:
    """Return the replaceable unquoted mention fragment under the cursor."""

    prefix = value[:cursor]
    at = prefix.rfind("@")
    if at < 0 or (at > 0 and not (value[at - 1].isspace() or value[at - 1] in "([{,:")):
        return None
    fragment = prefix[at + 1 :]
    if fragment.startswith('"'):
        fragment = fragment[1:]
        if '"' in fragment:
            return None
        close = value.find('"', cursor)
        end = close + 1 if close >= 0 else cursor
    elif any(character.isspace() for character in fragment):
        return None
    else:
        end = cursor
        while end < len(value) and not value[end].isspace():
            end += 1
    return at, end, fragment


def format_path_mention(path: str) -> str:
    """Format a selected path as input syntax, quoting spaces when required."""

    if any(character.isspace() for character in path):
        return f'@"{path}"'
    return f"@{path}"
