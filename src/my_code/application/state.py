"""Explicit ownership graph for mutable runtime state."""

import asyncio
from dataclasses import dataclass
from pathlib import Path

from my_code.model.capabilities import ActiveModelEnvironment
from my_code.permissions.policy import PermissionPolicy
from my_code.providers.router import ProviderConnection, ProviderRouter
from my_code.sessions.session import Session
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


@dataclass(slots=True)
class ProviderRuntime:
    """Atomically switched provider connection, client, and model environment."""

    router: ProviderRouter
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
        self._environment = environment

    async def close(self) -> None:
        await self.router.close()


class AppState:
    """Single runtime entry for workspace, Session, permissions, and Provider."""

    def __init__(
        self,
        *,
        workspace: WorkspaceState,
        session: Session,
        permissions: PermissionState,
        provider: ProviderRuntime,
    ) -> None:
        self.workspace = workspace
        self._session = session
        self.permissions = permissions
        self.provider = provider
        self._operation_lock = asyncio.Lock()

    @property
    def session(self) -> Session:
        return self._session

    def replace_session(self, candidate: Session) -> None:
        self._session = candidate

    def operation_lock(self) -> asyncio.Lock:
        return self._operation_lock

    async def close(self) -> None:
        async with self._operation_lock:
            await self.provider.close()


__all__ = [
    "AppState",
    "PermissionState",
    "ProviderRuntime",
    "WorkspaceState",
]
