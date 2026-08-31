"""Explicit ownership graph for mutable runtime state."""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from my_code.context.session_cache import SessionContextCache
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
from my_code.tools.catalog import ToolCatalog
from my_code.workspace.local import Workspace


@dataclass(slots=True)
class PermissionRuntime:
    """The sole runtime permission state, including process-local UI consent."""

    policy: PermissionPolicy
    sandbox_active: bool = False
    execution_environment: str = "local"
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

    def request_cycle(
        self, persist: Callable[[PermissionMode], object]
    ) -> tuple[PermissionMode, bool]:
        return self.request_mode(self.next_mode(), persist)

    def request_mode(
        self,
        target: PermissionMode,
        persist: Callable[[PermissionMode], object],
    ) -> tuple[PermissionMode, bool]:
        """Select one interactive mode without exposing policy mutation to the UI."""

        if target not in self._CYCLE:
            raise ValueError(f"Unsupported interactive permission mode: {target.value}")
        if target is self.policy.mode:
            return target, False
        if self.requires_full_access_confirmation(target):
            self.full_access_pending = True
            return target, True
        persist(target)
        self.policy.mode = target
        return target, False

    def confirm_full_access(
        self, allow: bool, persist: Callable[[PermissionMode], object]
    ) -> PermissionMode:
        if not self.full_access_pending:
            return self.policy.mode
        if allow:
            persist(PermissionMode.BYPASS)
            self.full_access_confirmed = True
            self.policy.mode = PermissionMode.BYPASS
        else:
            persist(self.policy.mode)
        self.full_access_pending = False
        return self.policy.mode

    def restore_mode(self, mode: PermissionMode) -> None:
        """Restore a trusted persisted mode without replaying UI confirmation."""

        self.policy.mode = mode
        self.full_access_pending = False
        if mode is PermissionMode.BYPASS:
            self.full_access_confirmed = True

    def restore_policy(self, candidate: PermissionPolicy) -> None:
        self.policy.rules = candidate.rules
        self.restore_mode(candidate.mode)


@dataclass(frozen=True, slots=True)
class ActiveSessionBinding:
    """一次原子发布的前台 Session、Cache、Run 与权限绑定。"""

    session: Session
    context_cache: SessionContextCache
    permission_policy: PermissionPolicy
    run_id: str

    @classmethod
    def build(
        cls,
        session: Session,
        permission_policy: PermissionPolicy,
    ) -> "ActiveSessionBinding":
        return cls(session, SessionContextCache(), permission_policy, session.run_id)


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
        previous_connection = self.router.connection
        previous_environment = self._environment
        await self.router.switch(connection)
        try:
            self.leases.switch(connection)
            self._environment = environment
        except BaseException:
            await self.router.switch(previous_connection)
            self.leases.switch(previous_connection)
            self._environment = previous_environment
            raise

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


class ApplicationRuntime:
    """Single runtime entry for workspace, Session, permissions, and Provider."""

    def __init__(
        self,
        *,
        workspace: Workspace,
        session: Session,
        permissions: PermissionRuntime,
        provider: ProviderRuntime,
        tools: ToolCatalog,
        tasks: TaskSupervisor,
        runs: AgentRunFactory,
        mcp: McpRuntime,
        skills: SkillRuntime,
        shutdown_observability: Callable[[], None] | None = None,
    ) -> None:
        self.workspace = workspace
        self.permissions = permissions
        self._foreground = ActiveSessionBinding.build(session, permissions.policy)
        self.provider = provider
        self.tools = tools
        self.tasks = tasks
        self.runs = runs
        self.mcp = mcp
        self.skills = skills
        self._shutdown_observability = shutdown_observability or (lambda: None)
        self._close_foreground: Callable[[], Awaitable[None]] | None = None
        self._operation_lock = asyncio.Lock()
        self._closed = False

    @property
    def session(self) -> Session:
        return self._foreground.session

    @property
    def context_cache(self) -> SessionContextCache:
        return self._foreground.context_cache

    @property
    def foreground(self) -> ActiveSessionBinding:
        return self._foreground

    def publish_foreground(self, candidate: ActiveSessionBinding) -> None:
        """完整恢复权限后，一次替换所有 session-scoped 身份与 cache。"""

        self.permissions.policy = candidate.permission_policy
        self.permissions.restore_mode(candidate.permission_policy.mode)
        self._foreground = candidate

    def build_foreground(
        self,
        session: Session,
        permission_policy: PermissionPolicy,
    ) -> ActiveSessionBinding:
        return ActiveSessionBinding.build(session, permission_policy)

    def operation_lock(self) -> asyncio.Lock:
        return self._operation_lock

    def bind_foreground_closer(self, close: Callable[[], Awaitable[None]]) -> None:
        """由 composition root 一次绑定前台交互资源的关闭入口。"""

        if self._close_foreground is not None:
            raise RuntimeError("Foreground closer is already bound")
        self._close_foreground = close

    async def start(self) -> None:
        """Lazily initialize optional application capabilities before a turn."""

        if self._closed:
            raise RuntimeError("Application runtime is closed")
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(self.mcp.start())
            tasks.create_task(self.skills.start())

    async def close(self) -> None:
        async with self._operation_lock:
            if self._closed:
                return
            self._closed = True
            errors: list[Exception] = []
            if self._close_foreground is not None:
                try:
                    await self._close_foreground()
                except Exception as error:
                    errors.append(error)
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
            try:
                self._shutdown_observability()
            except Exception as error:
                errors.append(error)
            if errors:
                raise ExceptionGroup("Failed to close application runtime", errors)


__all__ = [
    "ApplicationRuntime",
    "ActiveSessionBinding",
    "PermissionRuntime",
    "ProviderRuntime",
]
