"""First-run provider setup using the same storage-free wizard state as the TUI."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import WordCompleter

from my_code.config.providers import (
    ANTHROPIC_API_BASE_URL,
    OPENAI_API_BASE_URL,
    ProviderProtocol,
)
from my_code.config.validation import validate_base_url
from my_code.model.primitives import validate_provider_id
from my_code.providers.manager import ProviderManager
from my_code.tui.provider_screen import ProviderWizard

type Prompt = Callable[..., Awaitable[str]]


class ProviderSetupTui:
    """Small pre-runtime host for :class:`ProviderWizard`."""

    def __init__(
        self,
        manager: ProviderManager,
        *,
        prompt: Prompt | None = None,
    ) -> None:
        self.manager = manager
        self._session: PromptSession[str] | None = None
        if prompt is None:
            self._session = PromptSession()
            self._prompt = self._session.prompt_async
        else:
            self._prompt = prompt

    async def run(self, requested_provider: str | None = None) -> bool:
        profiles = self.manager.profiles.load()
        if requested_provider is not None:
            if requested_provider not in profiles:
                choices = ", ".join(sorted(profiles)) or "<none>"
                raise ValueError(
                    f"Unknown provider {requested_provider!r}; configured providers: "
                    f"{choices}"
                )
            self.manager.select_provider(requested_provider)
            return True

        try:
            if profiles:
                choices = ", ".join(sorted(profiles))
                selected = (
                    await self._prompt(f"Select provider ({choices}) or type 'add': ")
                ).strip()
                if selected and selected != "add":
                    self.manager.select_provider(selected)
                    return True
            return await self._add_provider()
        except (EOFError, KeyboardInterrupt):
            return False

    async def _add_provider(self) -> bool:
        wizard = ProviderWizard.new()
        form = wizard.form
        try:
            protocol = ""
            while protocol not in {"1", "2"}:
                protocol = (
                    await self._prompt(
                        "Protocol [1 Anthropic Messages / 2 OpenAI Responses]: "
                    )
                ).strip()
            form.protocol = (
                ProviderProtocol.OPENAI_RESPONSES.value
                if protocol == "2"
                else ProviderProtocol.ANTHROPIC_MESSAGES.value
            )
            while not form.provider_id:
                candidate = (await self._prompt("Provider ID: ")).strip()
                try:
                    validate_provider_id(candidate)
                except ValueError as error:
                    print(error)
                else:
                    form.provider_id = candidate
            official = (
                OPENAI_API_BASE_URL
                if form.protocol == ProviderProtocol.OPENAI_RESPONSES.value
                else ANTHROPIC_API_BASE_URL
            )
            form.base_url = (
                await self._prompt(f"Base URL [official: {official}]: ")
            ).strip()
            if form.base_url:
                validate_base_url(form.base_url)
            form.api_key = (
                await self._prompt("API key (optional): ", is_password=True)
            ).strip()

            while True:
                result = await self.manager.probe(wizard.probe_request())
                wizard.accept_probe(result)
                if result.succeeded:
                    ids = [item.id for item in result.models]
                    form.model = (
                        await self._prompt(
                            "Model: ",
                            completer=WordCompleter(ids, ignore_case=True),
                            complete_while_typing=True,
                        )
                    ).strip()
                    if form.model not in ids:
                        print("Choose a model returned by the provider catalog.")
                        continue
                    break
                print(result.error_message or "Connection check failed.")
                choice = (
                    (
                        await self._prompt(
                            "Retry, edit-url, edit-key, manual, or cancel: "
                        )
                    )
                    .strip()
                    .casefold()
                )
                if choice == "edit-url":
                    form.base_url = (await self._prompt("Base URL: ")).strip()
                elif choice == "edit-key":
                    form.api_key = (
                        await self._prompt("API key (optional): ", is_password=True)
                    ).strip()
                elif choice == "manual":
                    wizard.use_manual_model(await self._prompt("Model: "))
                    break
                elif choice == "cancel":
                    return False

            verified = "verified" if wizard.connection_verified else "not verified"
            confirm = (
                (
                    await self._prompt(
                        f"Save & use {form.provider_id}/{form.model} "
                        f"({verified})? [y/N]: "
                    )
                )
                .strip()
                .casefold()
            )
            if confirm not in {"y", "yes"}:
                return False
            self.manager.configure(
                wizard.build_update(),
                probe_result=(
                    wizard.probe_result if wizard.connection_verified else None
                ),
            )
            return True
        finally:
            wizard.clear_sensitive()


__all__ = ["ProviderSetupTui"]
