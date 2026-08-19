"""使用密码安全 API key 输入框的 provider profile 编辑器。"""

from collections.abc import Awaitable, Callable

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, OptionList, Select, Switch
from textual.widgets.option_list import Option

from my_code.config.providers import (
    CompactConfig,
    ProviderProfile,
    ProviderProtocol,
    ReasoningConfig,
)
from my_code.model.capabilities import ModelLimits
from my_code.providers.manager import ProviderUpdate, ProviderView


class ProviderScreen(ModalScreen[ProviderUpdate | None]):
    """编辑或创建一个 profile；持久化仍由运行时负责。"""

    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]

    def __init__(
        self,
        providers: tuple[ProviderView, ...],
        refresh_models: Callable[[str], Awaitable[ProviderView]] | None = None,
    ) -> None:
        super().__init__()
        self.providers = providers
        self._by_id = {provider.id: provider for provider in providers}
        self._refresh_models = refresh_models

    def compose(self) -> ComposeResult:
        options = [
            Option(
                f"{'●' if provider.active else ' '} {provider.id}",
                id=provider.id,
            )
            for provider in self.providers
        ]
        with Vertical(id="provider-dialog"):
            yield Label("Provider profiles", id="provider-title")
            yield Label(
                "Choose a provider protocol and its reasoning policy.",
                id="provider-description",
            )
            with Horizontal(id="provider-content"):
                yield OptionList(*options, id="provider-list", compact=True)
                with Vertical(id="provider-form"):
                    yield Label("Provider ID")
                    yield Input(id="provider-id", placeholder="company-gateway")
                    yield Label("Protocol")
                    yield Select(
                        (
                            (
                                "Anthropic Messages",
                                ProviderProtocol.ANTHROPIC_MESSAGES.value,
                            ),
                            (
                                "OpenAI Responses",
                                ProviderProtocol.OPENAI_RESPONSES.value,
                            ),
                        ),
                        id="provider-protocol",
                        allow_blank=False,
                        value=ProviderProtocol.ANTHROPIC_MESSAGES.value,
                    )
                    yield Label("Base URL (blank uses SDK default)")
                    yield Input(
                        id="provider-url",
                        placeholder="https://gateway.example/anthropic",
                    )
                    yield Label("Model")
                    yield Input(id="provider-model", placeholder="model name")
                    yield Label("Choose a discovered model (optional)")
                    yield Select(
                        (),
                        id="provider-model-list",
                        prompt="Use manual model input",
                    )
                    yield Label("Discovered models: none", id="provider-models-status")
                    yield Label("Context window tokens (optional)")
                    yield Input(id="provider-context-window", placeholder="Auto")
                    yield Label("Max input tokens (optional)")
                    yield Input(id="provider-max-input", placeholder="Auto")
                    yield Label("Max output tokens (optional)")
                    yield Input(id="provider-max-output", placeholder="Auto")
                    yield Label("Compact input-token trigger (blank = Auto)")
                    yield Input(id="provider-compact-trigger", placeholder="Auto (90%)")
                    yield Label("Enable reasoning")
                    yield Switch(True, id="provider-reasoning-enabled")
                    yield Label("Reasoning effort")
                    yield Input(id="provider-reasoning-effort", value="auto")
                    yield Label("Responses context (auto/current_turn/all_turns)")
                    yield Input(id="provider-reasoning-context", value="auto")
                    yield Label("API Key")
                    yield Input(
                        id="provider-key",
                        placeholder="Leave blank to keep the stored key",
                        password=True,
                    )
                    yield Label("", id="provider-key-status")
                    yield Label("", id="provider-error")
            with Horizontal(id="provider-actions"):
                yield Button("New", id="provider-new")
                yield Button("Refresh Models", id="provider-refresh")
                yield Button("Save & Use", id="provider-save", variant="primary")
                yield Button("Cancel", id="provider-cancel")

    def on_mount(self) -> None:
        selected = next(
            (provider for provider in self.providers if provider.active),
            self.providers[0] if self.providers else None,
        )
        if selected is None:
            self._new_profile()
            return
        self.query_one("#provider-list", OptionList).highlighted = list(
            self._by_id
        ).index(selected.id)
        self._load_profile(selected)

    @on(OptionList.OptionSelected, "#provider-list")
    def select_provider(self, event: OptionList.OptionSelected) -> None:
        if event.option_id is not None:
            self._load_profile(self._by_id[str(event.option_id)])

    @on(Button.Pressed, "#provider-new")
    def new_provider(self) -> None:
        self._new_profile()

    @on(Select.Changed, "#provider-model-list")
    def select_discovered_model(self, event: Select.Changed) -> None:
        if isinstance(event.value, str) and event.value:
            self.query_one("#provider-model", Input).value = event.value

    @on(Button.Pressed, "#provider-save")
    def save_provider(self) -> None:
        provider_id = self.query_one("#provider-id", Input).value.strip()
        base_url = self.query_one("#provider-url", Input).value.strip() or None
        model = self.query_one("#provider-model", Input).value.strip()
        api_key = self.query_one("#provider-key", Input).value.strip() or None
        protocol = ProviderProtocol(
            str(self.query_one("#provider-protocol", Select).value)
        )
        reasoning = ReasoningConfig(
            self.query_one("#provider-reasoning-enabled", Switch).value,
            self.query_one("#provider-reasoning-effort", Input).value.strip() or "auto",
            self.query_one("#provider-reasoning-context", Input).value.strip()
            or "auto",
        )
        try:
            limits = ModelLimits(
                self._optional_positive("#provider-context-window"),
                self._optional_positive("#provider-max-input"),
                self._optional_positive("#provider-max-output"),
            )
            compact = CompactConfig(
                self._optional_positive("#provider-compact-trigger")
            )
            ProviderProfile(
                id=provider_id,
                model=model,
                protocol=protocol,
                base_url=base_url,
                reasoning=reasoning,
                limits=limits,
                compact=compact,
            )
            if api_key is not None and any(char.isspace() for char in api_key):
                raise ValueError("API key must not contain whitespace")
        except ValueError as error:
            self.query_one("#provider-error", Label).update(str(error))
            return
        self.dismiss(
            ProviderUpdate(
                id=provider_id,
                model=model,
                base_url=base_url,
                api_key=api_key,
                protocol=protocol,
                reasoning=reasoning,
                limits=limits,
                compact=compact,
            )
        )

    @on(Button.Pressed, "#provider-refresh")
    async def refresh_provider_models(self) -> None:
        provider_id = self.query_one("#provider-id", Input).value.strip()
        if not provider_id or self._refresh_models is None:
            self.query_one("#provider-error", Label).update(
                "Save the provider before refreshing models."
            )
            return
        button = self.query_one("#provider-refresh", Button)
        button.disabled = True
        self.query_one("#provider-models-status", Label).update("Loading models…")
        try:
            provider = await self._refresh_models(provider_id)
            self._by_id[provider.id] = provider
            self._show_discovery(provider)
        except Exception as error:
            self.query_one("#provider-error", Label).update(str(error))
        finally:
            button.disabled = False

    @on(Button.Pressed, "#provider-cancel")
    def cancel_provider(self) -> None:
        self.action_cancel()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _new_profile(self) -> None:
        provider_id = self.query_one("#provider-id", Input)
        provider_id.disabled = False
        provider_id.value = ""
        self.query_one("#provider-url", Input).value = ""
        self.query_one("#provider-model", Input).value = ""
        self.query_one(
            "#provider-protocol", Select
        ).value = ProviderProtocol.ANTHROPIC_MESSAGES.value
        self.query_one("#provider-reasoning-enabled", Switch).value = True
        self.query_one("#provider-reasoning-effort", Input).value = "auto"
        self.query_one("#provider-reasoning-context", Input).value = "auto"
        self.query_one("#provider-key", Input).value = ""
        for selector in (
            "#provider-context-window",
            "#provider-max-input",
            "#provider-max-output",
            "#provider-compact-trigger",
        ):
            self.query_one(selector, Input).value = ""
        self.query_one("#provider-models-status", Label).update(
            "Discovered models: none"
        )
        self.query_one("#provider-model-list", Select).set_options(())
        self.query_one("#provider-key-status", Label).update("New provider")
        self.query_one("#provider-error", Label).update("")
        provider_id.focus()

    def _load_profile(self, provider: ProviderView) -> None:
        provider_id = self.query_one("#provider-id", Input)
        provider_id.value = provider.id
        provider_id.disabled = True
        self.query_one("#provider-url", Input).value = provider.base_url or ""
        self.query_one("#provider-model", Input).value = provider.model
        self.query_one("#provider-protocol", Select).value = provider.protocol.value
        self.query_one(
            "#provider-reasoning-enabled", Switch
        ).value = provider.reasoning.enabled
        self.query_one(
            "#provider-reasoning-effort", Input
        ).value = provider.reasoning.effort
        self.query_one(
            "#provider-reasoning-context", Input
        ).value = provider.reasoning.context
        self.query_one("#provider-key", Input).value = ""
        self.query_one("#provider-context-window", Input).value = _number_text(
            provider.limits.context_window_tokens
        )
        self.query_one("#provider-max-input", Input).value = _number_text(
            provider.limits.max_input_tokens
        )
        self.query_one("#provider-max-output", Input).value = _number_text(
            provider.limits.max_output_tokens
        )
        self.query_one("#provider-compact-trigger", Input).value = _number_text(
            provider.compact.trigger_input_tokens
        )
        key_status = (
            "Stored key configured" if provider.has_stored_key else "No stored key"
        )
        self.query_one("#provider-key-status", Label).update(key_status)
        self.query_one("#provider-error", Label).update("")
        self._show_discovery(provider)

    def _show_discovery(self, provider: ProviderView) -> None:
        details = f"Discovered models: {len(provider.models)}"
        if provider.capability_source:
            details += f" · source {provider.capability_source}"
        if provider.discovered_at:
            details += f" · {provider.discovered_at}"
        self.query_one("#provider-models-status", Label).update(details)
        model_list = self.query_one("#provider-model-list", Select)
        model_list.set_options((model, model) for model in provider.models)
        if provider.model in provider.models:
            model_list.value = provider.model
        self.query_one("#provider-error", Label).update(
            provider.discovery_error or provider.warning or ""
        )

    def _optional_positive(self, selector: str) -> int | None:
        value = self.query_one(selector, Input).value.strip()
        if not value:
            return None
        try:
            parsed = int(value)
        except ValueError as error:
            raise ValueError(f"{selector[1:]} must be a positive integer") from error
        if parsed < 1:
            raise ValueError(f"{selector[1:]} must be a positive integer")
        return parsed


def _number_text(value: int | None) -> str:
    return "" if value is None else str(value)


__all__ = [
    "ProviderScreen",
]
