"""Provider configuration operations with atomic runtime switching."""

from dataclasses import replace

from my_code.config.settings import AgentSettings
from my_code.model.capabilities import (
    ActiveModelEnvironment,
    CapabilitySource,
    ModelDescriptor,
    resolve_environment,
)
from my_code.providers.discovery import resolve_without_network
from my_code.providers.manager import (
    ModelView,
    ProviderManager,
    ProviderProbeRequest,
    ProviderProbeResult,
    ProviderUpdate,
    ProviderView,
)
from my_code.providers.router import ProviderConnection
from my_code.runtime.application import ProviderRuntime


def _identity(connection: ProviderConnection) -> tuple[str, object, str, str | None]:
    return (
        connection.id,
        connection.protocol,
        connection.model,
        connection.base_url,
    )


class ProviderOperations:
    def __init__(
        self,
        manager: ProviderManager,
        runtime: ProviderRuntime,
        settings: AgentSettings,
    ) -> None:
        self._manager = manager
        self._runtime = runtime
        self._settings = settings

    def replace_manager(self, manager: ProviderManager) -> None:
        self._manager = manager

    @property
    def manager(self) -> ProviderManager:
        return self._manager

    def providers(self) -> tuple[ProviderView, ...]:
        return self._manager.list(self._runtime.router.connection.id)

    def models(self) -> tuple[ModelView, ...]:
        current = self._runtime.router.connection.id
        view = next(item for item in self._manager.list(current) if item.id == current)
        return view.model_catalog

    def initialize_environment(
        self, connection: ProviderConnection, descriptor: ModelDescriptor
    ) -> ActiveModelEnvironment | None:
        """Publish startup metadata only if the captured connection is still active."""

        if _identity(self._runtime.router.connection) != _identity(connection):
            return None
        environment = resolve_environment(
            descriptor,
            requested_output_tokens=self._settings.max_output_tokens,
            configured_trigger_tokens=connection.compact.trigger_input_tokens,
            discovered_at=descriptor.discovered_at,
        )
        if connection.warning is not None:
            environment = replace(environment, warning=connection.warning)
        self._runtime.update_environment(environment)
        return environment

    async def refresh_models(self, provider_id: str) -> ProviderView:
        view = await self._manager.refresh_models(provider_id)
        current = self._runtime.router.connection
        if current.id != provider_id:
            return view
        refreshed = self._manager.resolve(provider_id)
        descriptor = refreshed.model_descriptor or ModelDescriptor(
            view.model,
            view.model,
            view.resolved_limits,
            source=(
                CapabilitySource(view.capability_source)
                if view.capability_source is not None
                else CapabilitySource.FALLBACK
            ),
        )
        environment = self._environment(
            refreshed,
            descriptor=descriptor,
            discovered_at=view.discovered_at,
            discovery_error=view.discovery_error,
        )
        if _identity(current) == _identity(refreshed):
            self._runtime.update_environment(environment)
        else:
            await self._runtime.switch(refreshed, environment)
        return view

    async def probe(self, request: ProviderProbeRequest) -> ProviderProbeResult:
        return await self._manager.probe(request)

    async def select_provider(self, provider_id: str) -> ProviderConnection:
        connection = self._manager.select_provider(provider_id)
        await self._runtime.switch(connection, self._environment(connection))
        return connection

    async def select_model(self, model_id: str) -> ProviderConnection:
        current = self._runtime.router.connection
        connection = self._manager.select_model(current.id, model_id)
        try:
            await self._runtime.switch(connection, self._environment(connection))
        except BaseException:
            self._manager.select_model(current.id, current.model)
            raise
        return connection

    async def configure(
        self,
        update: ProviderUpdate,
        probe_result: ProviderProbeResult | None = None,
    ) -> ProviderConnection:
        connection = self._manager.configure(update, probe_result=probe_result)
        await self._runtime.switch(connection, self._environment(connection))
        return connection

    async def remove_credential(self, provider_id: str) -> ProviderConnection:
        removed = self._manager.delete_credential(provider_id)
        current = self._runtime.router.connection
        if not removed or current.id != provider_id:
            return current
        connection = self._manager.resolve(provider_id)
        await self._runtime.switch(connection, self._environment(connection))
        return connection

    def _environment(
        self,
        connection: ProviderConnection,
        *,
        descriptor: ModelDescriptor | None = None,
        discovered_at: str | None = None,
        discovery_error: str | None = None,
    ) -> ActiveModelEnvironment:
        resolved = (
            descriptor
            or connection.model_descriptor
            or resolve_without_network(
                connection.protocol,
                connection.base_url,
                connection.model,
                connection.limits,
            )
        )
        environment = resolve_environment(
            resolved,
            requested_output_tokens=self._settings.max_output_tokens,
            configured_trigger_tokens=connection.compact.trigger_input_tokens,
            discovered_at=discovered_at,
            discovery_error=discovery_error,
        )
        if connection.warning is not None:
            environment = replace(environment, warning=connection.warning)
        return environment


__all__ = ["ProviderOperations"]
