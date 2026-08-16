"""Provider-neutral user context supplied to model requests."""

from pathlib import Path
from typing import Protocol

from nano_code.messages import SystemContextBlock, UserContextMessage


class UserContextResolver(Protocol):
    """Resolve non-history context for model requests."""

    def resolve(self) -> tuple[UserContextMessage, ...]: ...


class AgentsUserContextResolver:
    """Load the workspace-root ``AGENTS.md`` as user context.

    Discovery is intentionally limited to the configured workspace root. The
    resolver does not walk parent directories, follow additional instruction
    conventions, or expand includes. ``ContextPlanner`` owns the session
    lifetime cache, so each resolver call represents one direct filesystem
    read.
    """

    def __init__(self, cwd: Path) -> None:
        self.cwd = cwd.resolve()

    def resolve(self) -> tuple[UserContextMessage, ...]:
        """Return the workspace instructions, or no context when absent."""

        path = self.cwd / "AGENTS.md"
        try:
            content = path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return ()

        if not content:
            return ()

        return (
            UserContextMessage(
                source="AGENTS.md",
                content=(
                    SystemContextBlock(
                        kind="system_reminder",
                        content=_format_agents_context(content),
                    ),
                ),
            ),
        )


class EmptyUserContextResolver:
    """Resolver for callers that explicitly do not provide user context."""

    def resolve(self) -> tuple[UserContextMessage, ...]:
        return ()


def _format_agents_context(content: str) -> str:
    return (
        "As you answer the user's questions, you can use the following context:\n"
        "# AGENTS.md\n"
        f"{content}\n\n"
        "IMPORTANT: this context may or may not be relevant to your tasks. "
        "You should not respond to this context unless it is highly relevant "
        "to your task."
    )


__all__ = [
    "AgentsUserContextResolver",
    "EmptyUserContextResolver",
    "UserContextResolver",
]
