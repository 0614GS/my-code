"""CLI 功能使用的 provider 与 session application adapters。"""

from pathlib import Path

from nano_code.agent.ports.session import SessionRepository
from nano_code.providers.catalog import (
    ActiveModelState,
    CapabilitySource,
    ModelDescriptor,
    resolve_environment,
)
from nano_code.providers.discovery import resolve_without_network
from nano_code.providers.manager import ProviderManager, ProviderUpdate, ProviderView
from nano_code.providers.router import ProviderConnection, ProviderRouter
from nano_code.sessions import SessionCatalog, SessionStore, SessionSummary


class CliProviderController:
    """组合 profile 持久化与活动 ProviderRouter 热切换。"""

    def __init__(
        self,
        manager: ProviderManager,
        router: ProviderRouter,
        active_model_state: ActiveModelState | None = None,
        max_output_tokens: int = 8192,
    ) -> None:
        self._manager = manager
        self._router = router
        self._active_model_state = active_model_state
        self._max_output_tokens = max_output_tokens

    def providers(self, active_provider_id: str) -> tuple[ProviderView, ...]:
        return self._manager.list(active_provider_id)

    async def refresh_models(self, provider_id: str) -> ProviderView:
        view = await self._manager.refresh_models(provider_id)
        if (
            self._active_model_state is not None
            and self._router.connection.id == provider_id
        ):
            source = (
                CapabilitySource(view.capability_source)
                if view.capability_source is not None
                else CapabilitySource.FALLBACK
            )
            self._active_model_state.set(
                resolve_environment(
                    ModelDescriptor(
                        view.model,
                        view.model,
                        view.resolved_limits,
                        source=source,
                    ),
                    requested_output_tokens=self._max_output_tokens,
                    configured_trigger_tokens=(
                        self._router.connection.compact.trigger_input_tokens
                    ),
                    discovered_at=view.discovered_at,
                    discovery_error=view.discovery_error,
                )
            )
        return view

    async def configure(self, update: ProviderUpdate) -> ProviderConnection:
        connection = self._manager.configure(update)
        await self._router.switch(connection)
        if self._active_model_state is not None:
            descriptor = resolve_without_network(
                connection.protocol,
                connection.base_url,
                connection.model,
                connection.limits,
            )
            self._active_model_state.set(
                resolve_environment(
                    descriptor,
                    requested_output_tokens=self._max_output_tokens,
                    configured_trigger_tokens=(connection.compact.trigger_input_tokens),
                )
            )
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
