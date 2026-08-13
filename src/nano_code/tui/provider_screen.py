"""使用密码安全 API key 输入框的 provider profile 编辑器。"""

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, OptionList
from textual.widgets.option_list import Option

from nano_code.providers.manager import ProviderUpdate, ProviderView
from nano_code.providers.profiles import ProviderProfile


class ProviderScreen(ModalScreen[ProviderUpdate | None]):
    """编辑或创建一个 profile；持久化仍由运行时负责。"""

    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]

    def __init__(self, providers: tuple[ProviderView, ...]) -> None:
        super().__init__()
        self.providers = providers
        self._by_id = {provider.id: provider for provider in providers}

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
                "All profiles use the Anthropic Messages API.",
                id="provider-description",
            )
            with Horizontal(id="provider-content"):
                yield OptionList(*options, id="provider-list", compact=True)
                with Vertical(id="provider-form"):
                    yield Label("Provider ID")
                    yield Input(id="provider-id", placeholder="company-gateway")
                    yield Label("Base URL (blank uses SDK default)")
                    yield Input(
                        id="provider-url",
                        placeholder="https://gateway.example/anthropic",
                    )
                    yield Label("Model")
                    yield Input(id="provider-model", placeholder="model name")
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

    @on(Button.Pressed, "#provider-save")
    def save_provider(self) -> None:
        provider_id = self.query_one("#provider-id", Input).value.strip()
        base_url = self.query_one("#provider-url", Input).value.strip() or None
        model = self.query_one("#provider-model", Input).value.strip()
        api_key = self.query_one("#provider-key", Input).value.strip() or None
        try:
            ProviderProfile(id=provider_id, model=model, base_url=base_url)
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
            )
        )

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
        self.query_one("#provider-key", Input).value = ""
        self.query_one("#provider-key-status", Label).update("New provider")
        self.query_one("#provider-error", Label).update("")
        provider_id.focus()

    def _load_profile(self, provider: ProviderView) -> None:
        provider_id = self.query_one("#provider-id", Input)
        provider_id.value = provider.id
        provider_id.disabled = True
        self.query_one("#provider-url", Input).value = provider.base_url or ""
        self.query_one("#provider-model", Input).value = provider.model
        self.query_one("#provider-key", Input).value = ""
        key_status = (
            "Stored key configured" if provider.has_stored_key else "No stored key"
        )
        self.query_one("#provider-key-status", Label).update(key_status)
        self.query_one("#provider-error", Label).update("")
