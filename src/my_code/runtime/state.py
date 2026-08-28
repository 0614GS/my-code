"""Explicit ownership graph for mutable runtime state."""

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

from my_code.context.session import ContextRuntime
from my_code.mcp.runtime import McpRuntime
from my_code.model.capabilities import ActiveModelEnvironment
from my_code.permissions.models import PermissionMode
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


@dataclass(slots=True)
class PermissionState:
    """The sole runtime permission state, including process-local UI consent."""

    policy: PermissionPolicy
    sandbox_active: bool = False
    full_access_confirmed: bool = False
    full_access_pending: bool = field(default=False, init=False)

    _CYCLE = (
        PermissionMode.DEFAULT,
        PermissionMode.ACCEPT_EDITS,
        PermissionMode.BYPASS,
    )

    def __post_init__(self) -> None:
        if self.requires_full_access_confirmation():
            # A configured bypass startup must not become active before the TUI
            # can show its process-local risk confirmation.
            self.policy.mode = PermissionMode.DEFAULT
            self.full_access_pending = True

    def next_mode(self) -> PermissionMode:
        try:
            index = self._CYCLE.index(self.policy.mode)
        except ValueError:
            return PermissionMode.DEFAULT
        return self._CYCLE[(index + 1) % len(self._CYCLE)]

    def requires_full_access_confirmation(
        self, mode: PermissionMode | None = None
    ) -> bool:
        candidate = self.policy.mode if mode is None else mode
        return (
            candidate is PermissionMode.BYPASS
            and not self.sandbox_active
            and not self.full_access_confirmed
        )

    def request_cycle(self) -> tuple[PermissionMode, bool]:
        target = self.next_mode()
        if self.requires_full_access_confirmation(target):
            self.full_access_pending = True
            return target, True
        self.policy.mode = target
        return target, False

    def confirm_full_access(self, allow: bool) -> PermissionMode:
        if not self.full_access_pending:
            return self.policy.mode
        self.full_access_pending = False
        if allow:
            self.full_access_confirmed = True
            self.policy.mode = PermissionMode.BYPASS
        return self.policy.mode


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

        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(self.mcp.start())
            tasks.create_task(self.skills.start())

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
