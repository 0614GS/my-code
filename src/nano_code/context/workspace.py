"""Provider-neutral workspace context supplied to model requests."""

from pathlib import Path
from typing import Protocol

from nano_code.agent.contracts.model import ModelMessage
from nano_code.messages import TextBlock


class WorkspaceContextResolver(Protocol):
    """Resolve non-history context that belongs to the current workspace."""

    def resolve(self) -> tuple[ModelMessage, ...]: ...


class AgentsWorkspaceContextResolver:
    """Load the workspace-root ``AGENTS.md`` as session context.

    Discovery is intentionally limited to the configured workspace root. The
    resolver does not walk parent directories, follow additional instruction
    conventions, or expand includes. ``ContextPlanner`` owns the session
    lifetime cache, so each resolver call represents one direct filesystem
    read.
    """

    def __init__(self, cwd: Path) -> None:
        self.cwd = cwd.resolve()

    def resolve(self) -> tuple[ModelMessage, ...]:
        """Return the workspace instructions, or no context when absent."""

        path = self.cwd / "AGENTS.md"
        try:
            content = path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return ()

        if not content:
            return ()

        return (
            ModelMessage(
                role="user",
                content=(TextBlock(_format_agents_context(content)),),
            ),
        )


class EmptyWorkspaceContextResolver:
    """Resolver for callers that explicitly do not provide workspace context."""

    def resolve(self) -> tuple[ModelMessage, ...]:
        return ()


def _format_agents_context(content: str) -> str:
    return (
        "<system-reminder>\n"
        "As you answer the user's questions, you can use the following context:\n"
        "# AGENTS.md\n"
        f"{content}\n\n"
        "IMPORTANT: this context may or may not be relevant to your tasks. "
        "You should not respond to this context unless it is highly relevant "
        "to your task.\n"
        "</system-reminder>\n"
    )


__all__ = [
    "AgentsWorkspaceContextResolver",
    "EmptyWorkspaceContextResolver",
    "WorkspaceContextResolver",
]
