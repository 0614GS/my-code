"""RUN-01 Agent runs use independent provider clients and stream locks."""

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from my_code.agent.engine import AgentEngine
from my_code.agent.models import AgentTurnInput, AgentTurnSucceeded
from my_code.application.runs import (
    AgentRunComponents,
    AgentRunFactory,
    AgentRunSpec,
)
from my_code.auth.credentials import CredentialSource
from my_code.config.providers import ProviderProtocol
from my_code.context.compaction import ContextCompactor
from my_code.context.engine import ContextEngine
from my_code.context.planner import ContextPlanner
from my_code.context.window import ContextWindow
from my_code.model.capabilities import (
    ActiveModelEnvironment,
    fallback_descriptor,
    resolve_environment,
)
from my_code.model.events import (
    ModelOutputCompleted,
    ModelStreamEvent,
    ModelStreamSequencer,
)
from my_code.model.primitives import TokenUsage
from my_code.model.request import (
    ModelOutput,
    ModelRequest,
    ModelTextBlock,
    PromptStability,
)
from my_code.permissions.models import PermissionMode
from my_code.permissions.policy import PermissionPolicy
from my_code.permissions.prompt import HeadlessPrompter
from my_code.prompts.models import PromptSection
from my_code.prompts.registry import PromptRegistry
from my_code.providers.leases import ProviderClientLease, ProviderLeaseRegistry
from my_code.providers.router import ProviderConnection
from my_code.sessions.session import Session
from my_code.tools.catalog import ToolCatalog
from my_code.tools.executor import ToolExecutor
from my_code.tools.round_executor import ToolRoundExecutor
from my_code.workspace.local import Workspace


class ConcurrentProvider:
    def __init__(
        self,
        entered: list[str],
        both_entered: asyncio.Event,
        release: asyncio.Event,
        provider_id: str,
    ) -> None:
        self.entered = entered
        self.both_entered = both_entered
        self.release = release
        self.provider_id = provider_id
        self.closed = False

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
        del request
        self.entered.append(self.provider_id)
        if len(self.entered) == 2:
            self.both_entered.set()
        await self.release.wait()
        output = ModelOutput(
            (ModelTextBlock(f"done:{self.provider_id}"),),
            "end_turn",
            TokenUsage(1, 1),
        )
        yield ModelStreamSequencer().emit(ModelOutputCompleted(output))

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_two_agent_runs_can_stream_concurrently(tmp_path: Path) -> None:
    entered: list[str] = []
    both_entered = asyncio.Event()
    release = asyncio.Event()
    next_client = 0
    built: list[ConcurrentProvider] = []

    def provider_factory(connection: ProviderConnection) -> ConcurrentProvider:
        nonlocal next_client
        next_client += 1
        provider = ConcurrentProvider(
            entered,
            both_entered,
            release,
            f"{connection.id}-{next_client}",
        )
        built.append(provider)
        return provider

    connection = ProviderConnection(
        id="test",
        protocol=ProviderProtocol.ANTHROPIC_MESSAGES,
        model="test-model",
        base_url=None,
        api_key=None,
        credential_source=CredentialSource.NONE,
    )
    leases = ProviderLeaseRegistry(connection, factory=provider_factory)
    environment = resolve_environment(
        fallback_descriptor("test-model"),
        requested_output_tokens=100,
        configured_trigger_tokens=None,
    )
    catalog = ToolCatalog()
    workspace = Workspace(tmp_path)

    def build(
        spec: AgentRunSpec,
        provider: ProviderClientLease,
        run_environment: ActiveModelEnvironment,
    ) -> AgentRunComponents:
        del spec
        executor = ToolExecutor(
            tools=catalog.snapshot(),
            policy=PermissionPolicy(PermissionMode.DEFAULT),
            prompter=HeadlessPrompter(),
            workspace=workspace,
        )
        context = ContextEngine(
            ContextPlanner(
                window=ContextWindow(10_000),
                prompt=PromptRegistry(
                    (
                        PromptSection(
                            "core",
                            PromptStability.STATIC,
                            lambda: "system",
                        ),
                    )
                ),
                max_output_tokens=100,
                binding_resolver=lambda: provider.binding,
                model_environment=lambda: run_environment,
            ),
            ContextCompactor(provider),
        )
        return AgentRunComponents(
            AgentEngine(
                model_call=provider,
                tool_round=ToolRoundExecutor(executor),
                context=context,
                tool_catalog=catalog,
            ),
            context,
            executor,
        )

    factory = AgentRunFactory(leases, lambda: environment, build)
    first = await factory.create(
        AgentRunSpec(
            Session(
                tmp_path / "sessions",
                "11111111-1111-1111-1111-111111111111",
            ),
            "first",
        )
    )
    second = await factory.create(
        AgentRunSpec(
            Session(
                tmp_path / "sessions",
                "22222222-2222-2222-2222-222222222222",
            ),
            "second",
        )
    )
    first_task = asyncio.create_task(first.submit(AgentTurnInput("first")))
    second_task = asyncio.create_task(second.submit(AgentTurnInput("second")))

    await asyncio.wait_for(both_entered.wait(), timeout=1)
    assert set(entered) == {"test-1", "test-2"}
    release.set()
    first_result, second_result = await asyncio.gather(first_task, second_task)

    assert isinstance(first_result, AgentTurnSucceeded)
    assert isinstance(second_result, AgentTurnSucceeded)
    assert {first_result.text, second_result.text} == {
        "done:test-1",
        "done:test-2",
    }
    await factory.close()
    await leases.close()
    assert all(provider.closed for provider in built)
    assert factory.active_count == 0
    assert leases.active_count == 0
