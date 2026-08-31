"""Resume, provider, model, and agent panel workflows."""
# pyright: reportAttributeAccessIssue=false

from __future__ import annotations

import asyncio
from typing import cast

from prompt_toolkit.document import Document

from my_code.config.validation import validate_base_url
from my_code.model.primitives import validate_provider_id
from my_code.permissions.models import PermissionConfirmation
from my_code.tui.provider_screen import (
    PROVIDER_ADVANCED_FIELDS,
    PROVIDER_CORE_FIELDS,
    ProviderForm,
    ProviderWizard,
)
from my_code.tui.widgets import system_message, welcome


class PanelFlowMixin:
    async def _open_resume(self) -> None:
        self._sessions = await self.runtime.list_sessions()
        self._panel_index = 0
        self._open_panel("resume")

    def _open_provider(self) -> None:
        self._providers = self.runtime.providers()
        self._panel_index = next(
            (i for i, item in enumerate(self._providers) if item.active), 0
        )
        self._provider_selected_index = self._panel_index
        self._open_panel("provider_select")

    def _open_model_picker(self) -> None:
        self._models = self.runtime.models()
        self._panel_index = next(
            (index for index, item in enumerate(self._models) if item.current), 0
        )
        self._open_panel("model_select")

    def _open_view_picker(self) -> None:
        self._panel_index = 0 if self._display_density.view_mode == "concise" else 1
        self._open_panel("view_select")

    def _open_permission_picker(self) -> None:
        modes = self.runtime.permission_modes()
        if not modes:
            return
        self._panel_index = next(
            (index for index, mode in enumerate(modes) if mode.current), 0
        )
        self._open_panel("permission_mode_select")

    def _open_agents(self) -> None:
        self._agents = self.runtime.subagent_tasks()
        self._panel_index = 0
        self._agent_scroll = 0
        self._agent_task_id = None
        self._open_panel("agents")

    def _cycle_agent_view(self) -> None:
        if self._panel == "permission":
            return
        self._agents = self.runtime.subagent_tasks()
        if self._panel != "agents":
            self._saved_draft = self.buffer.text
            self._panel = "agents"
            self._agent_task_id = self._agents[0].task_id if self._agents else None
            self._panel_index = 0
        else:
            ids = [item.task_id for item in self._agents]
            if self._agent_task_id is None:
                self._agent_task_id = ids[0] if ids else None
            elif self._agent_task_id in ids and ids.index(
                self._agent_task_id
            ) + 1 < len(ids):
                self._agent_task_id = ids[ids.index(self._agent_task_id) + 1]
            else:
                self._close_panel()
                return
        self._agent_scroll = 0
        self._invalidate()

    def _scroll_agent(self, offset: int | None) -> None:
        if self._panel != "agents" or self._agent_task_id is None:
            return
        if offset is None:
            self._agent_scroll = 0
        else:
            self._agent_scroll = max(0, self._agent_scroll + offset)
        self._invalidate()

    def _open_panel(self, name: str) -> None:
        self._saved_draft = self.buffer.text
        self.buffer.set_document(Document(""), bypass_readonly=True)
        self._panel = name
        self._invalidate()

    def _close_panel(self) -> None:
        if self._panel == "agents":
            self._agent_task_id = None
        if self._panel == "plan_action":
            self._pending_plan = None
        self._panel = None
        if self._provider_wizard is not None:
            self._provider_wizard.clear_sensitive()
        self._provider_wizard = None
        self._provider_form = None
        self.buffer.set_document(
            Document(self._saved_draft, len(self._saved_draft)), bypass_readonly=True
        )
        self._invalidate()

    def _move_panel(self, offset: int) -> None:
        view = self._panel_view()
        if view is not None:
            self._panel_picker.move(view.rows, offset)
            self._invalidate()

    async def _panel_enter(self) -> None:
        view = self._panel_view()
        row = self._panel_picker.current(view.rows) if view is not None else None
        action = row.key if row is not None else None
        if self._panel == "full_access":
            self._resolve_full_access(action == "allow")
        elif self._panel == "question":
            value = self.buffer.text.strip()
            if self._question_other:
                if value:
                    self._answer_question(value)
            elif action == "__other__":
                self._question_other = True
                self.buffer.set_document(Document(""), bypass_readonly=True)
                self._invalidate()
            elif action is not None:
                self._answer_question(action)
        elif self._panel == "plan_action" and action is not None:
            if action == "stay":
                self._pending_plan = None
                self._close_panel()
            else:
                try:
                    self.runtime.start_plan_implementation(
                        fresh_context=action == "fresh"
                    )
                except Exception as error:
                    await self._write(
                        system_message(f"Plan handoff failed: {error}", error=True)
                    )
                    return
                self._pending_plan = None
                self._close_panel()
                self._refresh_status()
                if self._foreground_task is None or self._foreground_task.done():
                    self._foreground_task = self._spawn(self._run_interactive_inputs())
        elif self._panel == "permission":
            value = self.buffer.text.strip()
            if self._permission_mode == "select" and action is not None:
                self._choose_permission(action)
            elif self._permission_mode == "feedback" and value:
                self._resolve_permission(PermissionConfirmation(False, value))
        elif self._panel == "resume" and action is not None:
            try:
                resumed = await self.runtime.resume_session(action)
                self._status = resumed.status
                self._context_status = None
                self._status_warning = ""
                self._todos = resumed.status.todos
                self._panel = None
                self.buffer.set_document(Document(""), bypass_readonly=True)
                await self._write(welcome(resumed.status, self.theme), clear=True)
                await self._render_history(resumed.history)
            except Exception as error:
                await self._write(
                    system_message(
                        f"Failed to resume conversation: {error}", error=True
                    )
                )
                self._close_panel()
        elif self._panel == "model_select" and action is not None:
            try:
                status = await self.runtime.select_model(action)
            except Exception as error:
                await self._write(
                    system_message(f"Model selection failed: {error}", error=True)
                )
                return
            self._status = status
            context = self._refresh_context_status_if_visible()
            self._status_warning = context.warning or "" if context is not None else ""
            self._close_panel()
            await self._write(system_message(f"Using model {status.model!r}"))
        elif self._panel == "view_select" and action is not None:
            try:
                message = await self._change_view_mode(action)
            except Exception as error:
                await self._write(
                    system_message(f"View mode selection failed: {error}", error=True)
                )
                return
            self._close_panel()
            await self._write(system_message(message))
        elif self._panel == "permission_mode_select" and action is not None:
            try:
                switch = self.runtime.select_permission_mode(action)
            except Exception as error:
                await self._write(
                    system_message(
                        f"Permission mode selection failed: {error}", error=True
                    )
                )
                return
            if switch.requires_confirmation:
                self._panel = "full_access"
                self._panel_index = 0
                self._full_access_resolved = asyncio.Event()
                self._invalidate()
                return
            self._close_panel()
            self._refresh_status()
            await self._write(
                system_message(f"Permission mode · {switch.mode.display_name}")
            )
        elif self._panel == "provider_select" and action is not None:
            if action == "add":
                self._provider_selected_index = -1
                self._provider_wizard = ProviderWizard.new()
                self._provider_form = self._provider_wizard.form
                self._panel = "provider_protocol"
                self._panel_index = 0
                self._invalidate()
            else:
                provider_id = action.removeprefix("provider:")
                self._provider_selected_index = next(
                    i
                    for i, provider in enumerate(self._providers)
                    if provider.id == provider_id
                )
                self._panel = "provider_actions"
                self._panel_index = 0
                self.buffer.set_document(Document(""), bypass_readonly=True)
                self._invalidate()
        elif self._panel == "provider_actions" and action is not None:
            provider = self._providers[self._provider_selected_index]
            if action == "use":
                await self._select_provider(provider.id)
            elif action == "configure":
                self._provider_wizard = ProviderWizard.edit(provider)
                self._provider_form = self._provider_wizard.form
                self._panel = "provider_protocol"
                self._panel_index = (
                    0 if provider.protocol.value == "anthropic-messages" else 1
                )
                self._invalidate()
            elif action == "remove":
                self._panel = "provider_remove_credential"
                self._panel_index = 1
                self._invalidate()
            else:
                self._provider_back()
        elif self._panel == "provider_remove_credential" and action is not None:
            if action == "remove":
                await self._remove_provider_credential()
            else:
                self._provider_back()
        elif self._panel == "provider_protocol" and action is not None:
            assert self._provider_form is not None
            self._provider_form.protocol = action
            self._start_provider_connection_form()
        elif self._panel == "provider_form":
            await self._advance_provider(1)
        elif self._panel == "provider_review" and action is not None:
            if action == "save":
                await self._save_provider()
            elif action == "models":
                if (
                    self._provider_wizard is not None
                    and self._provider_wizard.probe_result is not None
                ):
                    self._show_probe_models()
                else:
                    self._start_provider_form(
                        self._provider_form, fields=(("model", "Model"),)
                    )
            elif action == "advanced":
                self._start_provider_form(
                    self._provider_form, fields=PROVIDER_ADVANCED_FIELDS
                )
            else:
                self._cancel_provider_wizard()
        elif self._panel == "provider_probe_failure" and action is not None:
            if action == "retry":
                await self._probe_provider()
            elif action in {"base_url", "api_key"}:
                self._start_provider_connection_form()
                self._provider_field = next(
                    i
                    for i, (name, _label) in enumerate(self._provider_fields)
                    if name == action
                )
                self._load_provider_field()
            elif action == "manual":
                self._start_provider_form(
                    self._provider_form, fields=(("model", "Model"),)
                )
            else:
                self._cancel_provider_wizard()
        elif self._panel == "provider_models" and action is not None:
            assert self._provider_form is not None
            self._provider_form.model = action
            if self._provider_wizard is not None:
                self._provider_wizard.model_filter = ""
            self._panel = "provider_review"
            self._panel_index = 0
            self.buffer.set_document(Document(""), bypass_readonly=True)
            self._invalidate()
        elif self._panel == "agents" and action is not None:
            if action == "main":
                self._close_panel()
            else:
                self._agent_task_id = action
                self._agent_scroll = 0
                self._invalidate()

    def _start_provider_form(
        self,
        form: ProviderForm | None,
        *,
        fields: tuple[tuple[str, str], ...] = PROVIDER_CORE_FIELDS,
    ) -> None:
        if form is None:
            return
        self._provider_form = form
        self._provider_fields = fields
        self._provider_field = 0
        self._panel = "provider_form"
        self._load_provider_field()

    def _start_provider_connection_form(self) -> None:
        wizard = self._provider_wizard
        if wizard is None:
            return
        fields = PROVIDER_CORE_FIELDS[1:] if wizard.editing else PROVIDER_CORE_FIELDS
        self._start_provider_form(wizard.form, fields=fields)

    def _load_provider_field(self) -> None:
        assert self._provider_form is not None
        name = self._provider_fields[self._provider_field][0]
        value = cast(str, getattr(self._provider_form, name))
        self.buffer.set_document(Document(value, len(value)), bypass_readonly=True)
        self._invalidate()

    async def _advance_provider(self, offset: int) -> None:
        form = self._provider_form
        if form is None:
            return
        name = self._provider_fields[self._provider_field][0]
        setattr(form, name, self.buffer.text)
        try:
            if name == "provider_id":
                validate_provider_id(form.provider_id.strip())
            elif name == "base_url" and form.base_url.strip():
                validate_base_url(form.base_url.strip())
            elif name == "api_key" and any(
                character.isspace() for character in form.api_key.strip()
            ):
                raise ValueError("API key must not contain whitespace")
        except ValueError as error:
            await self._write(system_message(f"Invalid provider: {error}", error=True))
            return
        target = self._provider_field + offset
        if target >= len(self._provider_fields):
            if self._provider_fields in {
                PROVIDER_CORE_FIELDS,
                PROVIDER_CORE_FIELDS[1:],
            }:
                await self._probe_provider()
            elif self._provider_fields == (("model", "Model"),):
                if self._provider_wizard is not None:
                    self._provider_wizard.use_manual_model(form.model)
                self._open_provider_review()
            else:
                self._open_provider_review()
            return
        self._provider_field = max(0, target)
        self._load_provider_field()

    async def _refresh_provider(self) -> None:
        form = self._provider_form
        if form is None:
            return
        if self._provider_selected_index < 0:
            await self._write(
                system_message("Save this provider before discovering models.")
            )
            return
        try:
            view = await self.runtime.refresh_provider_models(form.provider_id.strip())
            self._provider_models = view.models
        except Exception as error:
            await self._write(
                system_message(f"Model discovery failed: {error}", error=True)
            )
            return
        if not self._provider_models:
            await self._write(system_message("No models were discovered."))
            return
        self._panel = "provider_models"
        self._panel_index = next(
            (i for i, model in enumerate(self._provider_models) if model == form.model),
            0,
        )
        self.buffer.set_document(Document(""), bypass_readonly=True)
        self._invalidate()

    async def _probe_provider(self) -> None:
        wizard = self._provider_wizard
        if wizard is None:
            return
        try:
            request = wizard.probe_request()
        except Exception as error:
            await self._write(system_message(f"Invalid provider: {error}", error=True))
            return
        self._panel = "provider_checking"
        self.buffer.set_document(Document(""), bypass_readonly=True)
        self._provider_probe_task = cast(asyncio.Task[object], asyncio.current_task())
        self._invalidate()
        try:
            result = await self.runtime.probe_provider(request)
        except asyncio.CancelledError:
            self._start_provider_connection_form()
            self._provider_field = len(self._provider_fields) - 1
            self._load_provider_field()
            return
        finally:
            self._provider_probe_task = None
        wizard.accept_probe(result)
        if wizard.connection_verified:
            self._open_provider_review()
        else:
            self._panel = "provider_probe_failure"
            self._panel_index = 0
            self._invalidate()

    def _show_probe_models(self) -> None:
        wizard = self._provider_wizard
        if wizard is None or wizard.probe_result is None:
            return
        wizard.model_filter = ""
        self._provider_models = wizard.filtered_models()
        self._panel = "provider_models"
        self._panel_index = next(
            (
                i
                for i, model in enumerate(self._provider_models)
                if model == wizard.form.model
            ),
            0,
        )
        self.buffer.set_document(Document(""), bypass_readonly=True)
        self._invalidate()

    def _open_provider_review(self) -> None:
        self._panel = "provider_review"
        self._panel_index = 0
        self.buffer.set_document(Document(""), bypass_readonly=True)
        self._invalidate()

    async def _save_provider(self) -> None:
        form = self._provider_form
        if form is None:
            return
        try:
            update = (
                self._provider_wizard.build_update()
                if self._provider_wizard is not None
                else form.build_update()
            )
            probe_result = (
                self._provider_wizard.probe_result
                if self._provider_wizard is not None
                and self._provider_wizard.connection_verified
                else None
            )
            status = await self.runtime.configure_provider(update, probe_result)
        except Exception as error:
            await self._write(
                system_message(f"Provider configuration failed: {error}", error=True)
            )
            return
        self._status = status
        context = self._refresh_context_status_if_visible()
        self._status_warning = context.warning or "" if context is not None else ""
        self._panel = None
        if self._provider_wizard is not None:
            self._provider_wizard.clear_sensitive()
        self._provider_wizard = None
        self._provider_form = None
        self.buffer.set_document(
            Document(self._saved_draft, len(self._saved_draft)), bypass_readonly=True
        )
        await self._write(
            system_message(f"Using provider {status.provider_id!r} · {status.model}")
        )
        self._invalidate()

    async def _select_provider(self, provider_id: str) -> None:
        try:
            status = await self.runtime.select_provider(provider_id)
        except Exception as error:
            await self._write(
                system_message(f"Provider selection failed: {error}", error=True)
            )
            return
        self._status = status
        context = self._refresh_context_status_if_visible()
        self._status_warning = context.warning or "" if context is not None else ""
        self._close_panel()
        await self._write(
            system_message(f"Using provider {status.provider_id!r} · {status.model}")
        )

    async def _remove_provider_credential(self) -> None:
        provider = self._providers[self._provider_selected_index]
        try:
            status = await self.runtime.remove_provider_credential(provider.id)
            providers = self.runtime.providers()
        except Exception as error:
            await self._write(
                system_message(f"Failed to remove saved API key: {error}", error=True)
            )
            return
        self._status = status
        self._refresh_context_status_if_visible()
        self._providers = providers
        self._panel = None
        self.buffer.set_document(
            Document(self._saved_draft, len(self._saved_draft)), bypass_readonly=True
        )
        message = (
            f"Saved API key for {provider.id!r} removed. "
            "The provider is now not configured."
        )
        await self._write(system_message(message))
        self._invalidate()

    def _provider_back(self) -> None:
        if self._panel == "provider_actions":
            self._panel = "provider_select"
            self._panel_index = max(self._provider_selected_index, 0)
        elif self._panel == "provider_remove_credential":
            self._panel = "provider_actions"
            self._panel_index = 2
        elif self._panel == "provider_models":
            self._panel = "provider_review"
            self._panel_index = 1
        elif self._panel == "provider_checking":
            if self._provider_probe_task is not None:
                self._provider_probe_task.cancel()
            return
        elif self._panel == "provider_probe_failure":
            self._start_provider_connection_form()
            self._provider_field = len(self._provider_fields) - 1
            self._load_provider_field()
            return
        elif self._panel == "provider_form":
            if self._provider_fields in {
                PROVIDER_CORE_FIELDS,
                PROVIDER_CORE_FIELDS[1:],
            }:
                self._panel = "provider_protocol"
                self._panel_index = (
                    0
                    if self._provider_form is not None
                    and self._provider_form.protocol == "anthropic-messages"
                    else 1
                )
            else:
                self._open_provider_review()
                return
        elif self._panel == "provider_review":
            if (
                self._provider_wizard is not None
                and self._provider_wizard.probe_result is not None
            ):
                self._show_probe_models()
            else:
                self._start_provider_form(
                    self._provider_form, fields=(("model", "Model"),)
                )
            return
        elif self._panel == "provider_protocol":
            self._cancel_provider_wizard()
            return
        self.buffer.set_document(Document(""), bypass_readonly=True)
        self._invalidate()

    def _cancel_provider_wizard(self) -> None:
        if self._provider_wizard is not None:
            self._provider_wizard.clear_sensitive()
        self._provider_wizard = None
        self._provider_form = None
        if self._provider_selected_index >= 0:
            self._panel = "provider_actions"
            self._panel_index = 1
        else:
            self._panel = "provider_select"
            self._panel_index = len(self._providers)
        self.buffer.set_document(Document(""), bypass_readonly=True)
        self._invalidate()


__all__ = ["PanelFlowMixin"]
