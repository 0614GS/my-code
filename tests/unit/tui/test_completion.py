from my_code.tui.completion import format_path_mention, mention_at_cursor


def test_cursor_mention_and_quoted_insertion() -> None:
    assert mention_at_cursor("read @src/ma.py now", 12) == (5, 15, "src/ma")
    assert mention_at_cursor('read @"docs/main file.md"', 12) == (
        5,
        25,
        "docs/",
    )
    assert mention_at_cursor("me@example.com", 14) is None
    assert format_path_mention("docs/a file.md") == '@"docs/a file.md"'


def test_cursor_mention_requires_a_leading_space() -> None:
    assert mention_at_cursor("@src/main.py", 12) is None
    assert mention_at_cursor("read(@src/main.py", 17) is None
    assert mention_at_cursor("read\n@src/main.py", 17) is None
    assert mention_at_cursor("read  @src/main.py", 18) == (6, 18, "src/main.py")
