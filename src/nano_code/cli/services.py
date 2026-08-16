"""CLI 功能使用的 provider 与 session application adapters。"""

from pathlib import Path

from nano_code.agent.ports.session import SessionRepository
from nano_code.providers.manager import ProviderManager, ProviderUpdate, ProviderView
from nano_code.providers.router import ProviderConnection, ProviderRouter
from nano_code.sessions import SessionCatalog, SessionStore, SessionSummary


class CliProviderController:
    """组合 profile 持久化与活动 ProviderRouter 热切换。"""

    def __init__(self, manager: ProviderManager, router: ProviderRouter) -> None:
        self._manager = manager
        self._router = router

    def providers(self, active_provider_id: str) -> tuple[ProviderView, ...]:
        return self._manager.list(active_provider_id)

    async def configure(self, update: ProviderUpdate) -> ProviderConnection:
        connection = self._manager.configure(update)
        await self._router.switch(connection)
        return connection


class ProjectSessionSource:
    """当前项目的 session 发现与 repository factory。"""

    def __init__(self, project_state_dir: Path) -> None:
        self._project_state_dir = project_state_dir
        self._catalog = SessionCatalog(project_state_dir)

    def list(self, *, exclude_session_id: str) -> tuple[SessionSummary, ...]:
        return self._catalog.list(exclude_session_id=exclude_session_id)

    def open(self, session_id: str) -> SessionRepository:
        return SessionStore(self._project_state_dir, session_id)
