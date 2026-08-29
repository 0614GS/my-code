"""Minimal turn runner boundary used by application and runtime adapters."""

from collections.abc import AsyncIterator
from typing import Protocol

from my_code.agent.events import AgentEvent
from my_code.agent.models import AgentTurnInput, AgentTurnOutcome
from my_code.context.session import ContextRuntime
from my_code.sessions.session import Session


class AgentRunner(Protocol):
    async def submit(
        self, session: Session, runtime: ContextRuntime, turn_input: AgentTurnInput
    ) -> AgentTurnOutcome: ...

    def stream(
        self, session: Session, runtime: ContextRuntime, turn_input: AgentTurnInput
    ) -> AsyncIterator[AgentEvent]: ...

    def stream_continuation(
        self, session: Session, runtime: ContextRuntime
    ) -> AsyncIterator[AgentEvent]: ...


__all__ = ["AgentRunner"]
