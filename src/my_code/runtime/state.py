"""Explicit ownership graph for mutable runtime state."""

import asyncio
from dataclasses import dataclass
from pathlib import Path

from my_code.context.session import ContextRuntime
from my_code.mcp.runtime import McpRuntime
from my_code.model.capabilities import ActiveModelEnvironment
from my_code.permissions.policy import PermissionPolicy
from my_code.providers.leases import ProviderLeaseRegistry
from my_code.providers.router import ProviderConnection, ProviderRouter
from my_code.runtime.runs import AgentRunFactory
from my_code.sessions.session import Session
from my_code.skills.runtime import SkillRuntime
from my_code.tasks.supervisor import TaskSupervisor
from my_code.tools.catalog import ToolCatalog, ToolCatalogSnapshot
from my_code.workspace.local import Workspace


@dataclass(frozen=True, slots=True)
class WorkspaceState:
    workspace: Workspace

    @property
    def root(self) -> Path:
        return self.workspace.root


@dataclass(frozen=True, slots=True)
class PermissionState:
    """The sole runtime reference to the mutable permission policy."""

    policy: PermissionPolicy


@dataclass(frozen=True, slots=True)
class ToolState:
    """The application-lifetime dynamic Tool catalog."""

    catalog: ToolCatalog

    def snapshot(self) -> ToolCatalogSnapshot:
        return self.catalog.snapshot()


@dataclass(slots=True)
class ProviderRuntime:
    """Atomically switched provider connection, client, and model environment."""

    router: ProviderRouter
    leases: ProviderLeaseRegistry
    _environment: ActiveModelEnvironment

    def environment(self) -> ActiveModelEnvironment:
        return self._environment

    def update_environment(self, environment: ActiveModelEnvironment) -> None:
        self._environment = environment

    async def switch(
        self,
        connection: ProviderConnection,
        environment: ActiveModelEnvironment,
    ) -> None:
        await self.router.switch(connection)
        self.leases.switch(connection)
        self._environment = environment

    async def close(self) -> None:
        lease_error: Exception | None = None
        try:
            await self.leases.close()
        except Exception as error:
            lease_error = error
        try:
            await self.router.close()
        except Exception as error:
            if lease_error is not None:
                raise ExceptionGroup(
                    "Failed to close provider runtime",
                    (lease_error, error),
                ) from error
            raise
        if lease_error is not None:
            raise lease_error


class AppState:
    """Single runtime entry for workspace, Session, permissions, and Provider."""

    def __init__(
        self,
        *,
        workspace: WorkspaceState,
        session: Session,
        permissions: PermissionState,
        provider: ProviderRuntime,
        tools: ToolState,
        tasks: TaskSupervisor,
        runs: AgentRunFactory,
        mcp: McpRuntime,
        skills: SkillRuntime,
    ) -> None:
        self.workspace = workspace
        self._session = session
        self._context_runtime = ContextRuntime()
        self.permissions = permissions
        self.provider = provider
        self.tools = tools
        self.tasks = tasks
        self.runs = runs
        self.mcp = mcp
        self.skills = skills
        self._operation_lock = asyncio.Lock()

    @property
    def session(self) -> Session:
        return self._session

    @property
    def context_runtime(self) -> ContextRuntime:
        return self._context_runtime

    def replace_session(self, candidate: Session) -> None:
        self._session = candidate
        self._context_runtime = ContextRuntime()

    def operation_lock(self) -> asyncio.Lock:
        return self._operation_lock

    async def start(self) -> None:
        """Lazily initialize optional application capabilities before a turn."""

        await self.mcp.start()
        await self.skills.start()

    async def close(self) -> None:
        async with self._operation_lock:
            errors: list[Exception] = []
            for close in (
                self.tasks.close,
                self.runs.close,
                self.skills.close,
                self.mcp.close,
                self.provider.close,
            ):
                try:
                    await close()
                except Exception as error:
                    errors.append(error)
            if errors:
                raise ExceptionGroup("Failed to close application runtime", errors)


__all__ = [
    "AppState",
    "PermissionState",
    "ProviderRuntime",
    "ToolState",
    "WorkspaceState",
]
