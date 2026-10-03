"""Stable MCP public names retain distinct remote identities."""

from pathlib import Path

import pytest

from my_code.mcp.models import (
    McpAuthKind,
    McpServerSpec,
    McpServerTransport,
    public_tool_name,
)


def test_public_tool_names_are_distinct_stable_and_provider_bounded() -> None:
    assert public_tool_name("server", "a-b") == "mcp__server__a-b"
    assert public_tool_name("server", "a_b") == "mcp__server__a_b"
    assert public_tool_name("server", "a.b") == "mcp__server__a_dot_b"

    long_name = public_tool_name("s" * 64, "t" * 128)
    assert len(long_name) == 64
    assert long_name == public_tool_name("s" * 64, "t" * 128)
    assert long_name != public_tool_name("s" * 64, "u" * 128)


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/mcp",
        "https://user:secret@example.com/mcp",
        "https://example.com/mcp#fragment",
    ],
)
def test_remote_mcp_url_rejects_insecure_or_embedded_credentials(url: str) -> None:
    with pytest.raises(ValueError, match="HTTPS or loopback"):
        McpServerSpec(
            "remote", None, Path("/tmp"), transport=McpServerTransport.HTTP, url=url
        )


def test_bearer_mcp_requires_environment_variable_reference() -> None:
    with pytest.raises(ValueError, match="environment variable name"):
        McpServerSpec(
            "remote",
            None,
            Path("/tmp"),
            transport=McpServerTransport.HTTP,
            url="https://example.com/mcp",
            auth=McpAuthKind.BEARER,
            bearer_token_from="literal token",
        )
