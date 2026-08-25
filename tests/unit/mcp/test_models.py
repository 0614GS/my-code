"""Stable MCP public names retain distinct remote identities."""

from my_code.mcp.models import public_tool_name, tool_search_name


def test_public_tool_names_are_distinct_stable_and_provider_bounded() -> None:
    assert public_tool_name("server", "a-b") == "mcp__server__a-b"
    assert public_tool_name("server", "a_b") == "mcp__server__a_b"
    assert public_tool_name("server", "a.b") == "mcp__server__a_dot_b"

    long_name = public_tool_name("s" * 64, "t" * 128)
    assert len(long_name) == 64
    assert long_name == public_tool_name("s" * 64, "t" * 128)
    assert long_name != public_tool_name("s" * 64, "u" * 128)
    assert len(tool_search_name("s" * 64)) <= 64
