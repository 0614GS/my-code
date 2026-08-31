"""Minimal turn runner boundary used by application and runtime adapters."""

from collections.abc import AsyncIterator, Sequence
from typing import Protocol

from my_code.agent.events import AgentEvent
from my_code.agent.models import (
    AgentInvocationOutcome,
    AgentTurnInput,
    PendingInputSource,
    UserTurnInput,
)
from my_code.context.session_cache import SessionContextCache
from my_code.sessions.session import Session


class AgentRunner(Protocol):
    async def submit(
        self,
        session: Session,
        runtime: SessionContextCache,
        turn_input: AgentTurnInput,
    ) -> AgentInvocationOutcome: ...

    def stream(
        self,
        session: Session,
        runtime: SessionContextCache,
        turn_input: AgentTurnInput,
    ) -> AsyncIterator[AgentEvent]: ...

    def stream_continuation(
        self,
        session: Session,
        runtime: SessionContextCache,
    ) -> AsyncIterator[AgentEvent]: ...


class InteractiveAgentRunner(Protocol):
    """Extended runner surface used by interactive steering hosts."""

    async def submit(
        self,
        session: Session,
        runtime: SessionContextCache,
        turn_input: AgentTurnInput | Sequence[UserTurnInput],
        pending_source: PendingInputSource | None = None,
    ) -> AgentInvocationOutcome: ...

    def stream(
        self,
        session: Session,
        runtime: SessionContextCache,
        turn_input: AgentTurnInput | Sequence[UserTurnInput],
        pending_source: PendingInputSource | None = None,
    ) -> AsyncIterator[AgentEvent]: ...

    def stream_continuation(
        self,
        session: Session,
        runtime: SessionContextCache,
        pending_source: PendingInputSource | None = None,
    ) -> AsyncIterator[AgentEvent]: ...


__all__ = ["AgentRunner", "InteractiveAgentRunner"]
