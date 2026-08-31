"""Isolated Agent run capsules backed by independent provider leases."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from uuid import uuid4

from my_code.agent.events import AgentEvent
from my_code.agent.models import AgentTurnInput, AgentTurnOutcome
from my_code.agent.runner import InteractiveAgentRunner
from my_code.context.engine import ContextEngine
from my_code.context.session import ContextRuntime
from my_code.model.capabilities import ActiveModelEnvironment
from my_code.observability.api import EvaluationContext
from my_code.permissions.policy import PermissionPolicy
from my_code.prompts.registry import PromptRegistry
from my_code.providers.leases import ProviderClientLease, ProviderLeaseRegistry
from my_code.sessions.models import SessionStart
from my_code.sessions.session import Session
from my_code.tools.catalog import ToolCatalog
from my_code.tools.executor import ToolExecutor


@dataclass(frozen=True, slots=True)
class AgentRunSpec:
    """Explicit per-run identity and writable Session ownership."""

    session: Session
    name: str
    parent_run_id: str | None = None
    run_id: str = field(default_factory=lambda: str(uuid4()))
    tool_catalog: ToolCatalog | None = None
    permission_policy: PermissionPolicy | None = None
    prompt_registry: PromptRegistry | None = None
    max_steps: int | None = None
    max_tokens: int | None = None
    allow_permission_updates: bool = True
    evaluation: EvaluationContext | None = None

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("Agent run name must not be blank")
        if self.parent_run_id is not None and not self.parent_run_id.strip():
            raise ValueError("Agent parent run ID must be non-empty or null")
        if not self.run_id.strip():
            raise ValueError("Agent run ID must not be blank")
        if self.max_steps is not None and self.max_steps < 1:
            raise ValueError("Agent run max_steps must be positive or null")
        if self.max_tokens is not None and self.max_tokens < 1:
            raise ValueError("Agent run max_tokens must be positive or null")


@dataclass(frozen=True, slots=True)
class AgentRunComponents:
    agent: InteractiveAgentRunner
    context: ContextEngine
    tool_executor: ToolExecutor


type AgentRunBuilder = Callable[
    [AgentRunSpec, ProviderClientLease, ActiveModelEnvironment],
    AgentRunComponents,
]
type SessionStartResolver = Callable[
    [AgentRunSpec, ProviderClientLease, ActiveModelEnvironment], SessionStart
]


class AgentRun:
    """One Session, agent runner and provider lease with explicit close ownership."""

    def __init__(
        self,
        *,
        run_id: str,
        spec: AgentRunSpec,
        components: AgentRunComponents,
        provider: ProviderClientLease,
        environment: ActiveModelEnvironment,
        release: Callable[[str], None],
    ) -> None:
        self.run_id = run_id
        self.name = spec.name
        self.parent_run_id = spec.parent_run_id
        self.session = spec.session
        self.agent = components.agent
        self.context = components.context
        self.context_runtime = ContextRuntime()
        self.tool_executor = components.tool_executor
        self.provider = provider
        self.environment = environment
        self._release = release
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    async def submit(self, turn_input: AgentTurnInput) -> AgentTurnOutcome:
        if self._closed:
            raise RuntimeError("Agent run is closed")
        return await self.agent.submit(self.session, self.context_runtime, turn_input)

    def stream(self, turn_input: AgentTurnInput) -> AsyncIterator[AgentEvent]:
        if self._closed:
            raise RuntimeError("Agent run is closed")
        return self.agent.stream(self.session, self.context_runtime, turn_input)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self.provider.close()
        finally:
            self._release(self.run_id)


class AgentRunFactory:
    """Create and own run capsules without sharing provider stream locks."""

    def __init__(
        self,
        leases: ProviderLeaseRegistry,
        environment: Callable[[], ActiveModelEnvironment],
        build: AgentRunBuilder,
        session_start: SessionStartResolver | None = None,
    ) -> None:
        self._leases = leases
        self._environment = environment
        self._build = build
        self._session_start = session_start
        self._runs: dict[str, AgentRun] = {}
        self._accepting = True

    @property
    def active_count(self) -> int:
        return len(self._runs)

    async def create(self, spec: AgentRunSpec) -> AgentRun:
        if not self._accepting:
            raise RuntimeError("Agent run factory is closed")
        if spec.run_id in self._runs:
            raise ValueError(f"Agent run ID is already active: {spec.run_id}")
        environment = self._environment()
        provider = self._leases.acquire()
        try:
            if self._session_start is not None:
                start = self._session_start(spec, provider, environment)
                spec.session.configure_start(start)
            components = self._build(spec, provider, environment)
        except BaseException:
            await provider.close()
            raise
        run = AgentRun(
            run_id=spec.run_id,
            spec=spec,
            components=components,
            provider=provider,
            environment=environment,
            release=self._release,
        )
        self._runs[spec.run_id] = run
        return run

    async def close(self) -> None:
        if not self._accepting and not self._runs:
            return
        self._accepting = False
        results = await asyncio.gather(
            *(run.close() for run in tuple(self._runs.values())),
            return_exceptions=True,
        )
        cancellations = tuple(
            result for result in results if isinstance(result, asyncio.CancelledError)
        )
        if cancellations:
            raise cancellations[0]
        failures = tuple(result for result in results if isinstance(result, Exception))
        if failures:
            raise ExceptionGroup("Failed to close Agent runs", failures)

    def _release(self, run_id: str) -> None:
        self._runs.pop(run_id, None)


__all__ = [
    "AgentRun",
    "AgentRunBuilder",
    "AgentRunComponents",
    "AgentRunFactory",
    "AgentRunSpec",
]
