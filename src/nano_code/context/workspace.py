"""Provider-neutral workspace context supplied to model requests."""

from pathlib import Path
from typing import Protocol

from nano_code.agent.contracts.context import EphemeralContextMessage
from nano_code.messages import SystemContextBlock


class WorkspaceContextResolver(Protocol):
    """Resolve non-history context that belongs to the current workspace."""

    def resolve(self) -> tuple[EphemeralContextMessage, ...]: ...


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

    def resolve(self) -> tuple[EphemeralContextMessage, ...]:
        """Return the workspace instructions, or no context when absent."""

        path = self.cwd / "AGENTS.md"
        try:
            content = path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return ()

        if not content:
            return ()

        return (
            EphemeralContextMessage(
                role="user",
                content=(
                    SystemContextBlock(
                        kind="system_reminder",
                        content=_format_agents_context(content),
                    ),
                ),
            ),
        )


class EmptyWorkspaceContextResolver:
    """Resolver for callers that explicitly do not provide workspace context."""

    def resolve(self) -> tuple[EphemeralContextMessage, ...]:
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
    "AgentsWorkspaceContextResolver",
    "EmptyWorkspaceContextResolver",
    "WorkspaceContextResolver",
]
