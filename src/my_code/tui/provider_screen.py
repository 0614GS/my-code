"""State and validation for the native bottom provider editor."""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from my_code.config.providers import CompactConfig, ProviderProtocol, ReasoningConfig
from my_code.model.capabilities import ModelDescriptor, ModelLimits
from my_code.providers.manager import (
    ProviderProbeError,
    ProviderProbeRequest,
    ProviderProbeResult,
    ProviderUpdate,
    ProviderView,
)


@dataclass(slots=True)
class ProviderForm:
    """A frontend-local form; secrets only live in the password buffer."""

    provider_id: str = ""
    protocol: str = ProviderProtocol.ANTHROPIC_MESSAGES.value
    base_url: str = ""
    model: str = ""
    context_window: str = ""
    max_input: str = ""
    max_output: str = ""
    compact_trigger: str = ""
    reasoning_enabled: str = "yes"
    reasoning_effort: str = "auto"
    reasoning_context: str = "auto"
    api_key: str = field(default="", repr=False)

    @classmethod
    def from_view(cls, view: ProviderView) -> ProviderForm:
        return cls(
            provider_id=view.id,
            protocol=view.protocol.value,
            base_url=view.base_url or "",
            model=view.model,
            context_window=_number(view.limits.context_window_tokens),
            max_input=_number(view.limits.max_input_tokens),
            max_output=_number(view.limits.max_output_tokens),
            compact_trigger=_number(view.compact.trigger_input_tokens),
            reasoning_enabled="yes" if view.reasoning.enabled else "no",
            reasoning_effort=view.reasoning.effort,
            reasoning_context=view.reasoning.context,
        )

    def build_update(self) -> ProviderUpdate:
        key = self.api_key.strip() or None
        if key is not None and any(character.isspace() for character in key):
            raise ValueError("API key must not contain whitespace")
        protocol_name = self.protocol.strip().casefold()
        protocol_name = {
            "anthropic": ProviderProtocol.ANTHROPIC_MESSAGES.value,
            "openai": ProviderProtocol.OPENAI_RESPONSES.value,
        }.get(protocol_name, protocol_name)
        model_ids = tuple(dict.fromkeys(self.model.split()))
        if not model_ids:
            raise ValueError("At least one model ID is required")
        return ProviderUpdate(
            id=self.provider_id.strip(),
            protocol=ProviderProtocol(protocol_name),
            base_url=self.base_url.strip() or None,
            model=model_ids[0],
            limits=ModelLimits(
                _positive(self.context_window, "context window"),
                _positive(self.max_input, "max input"),
                _positive(self.max_output, "max output"),
            ),
            compact=CompactConfig(_positive(self.compact_trigger, "compact trigger")),
            reasoning=ReasoningConfig(
                self.reasoning_enabled.strip().casefold() in {"yes", "true", "1", "on"},
                self.reasoning_effort.strip() or "auto",
                self.reasoning_context.strip() or "auto",
            ),
            api_key=key,
            models=tuple(
                ModelDescriptor(item, user_defined=True) for item in model_ids
            ),
        )

    def build_update_for_probe(self) -> ProviderUpdate:
        """Validate connection fields before a model has been selected."""

        model = self.model
        try:
            self.model = "__probe__"
            return self.build_update()
        finally:
            self.model = model


PROVIDER_FIELDS: tuple[tuple[str, str], ...] = (
    ("provider_id", "Provider ID"),
    ("protocol", "Protocol (anthropic or openai)"),
    ("base_url", "Base URL (blank uses SDK default)"),
    ("model", "Model"),
    ("context_window", "Context window tokens (optional)"),
    ("max_input", "Max input tokens (optional)"),
    ("max_output", "Max output tokens (optional)"),
    ("compact_trigger", "Compact trigger tokens (optional)"),
    ("reasoning_enabled", "Reasoning enabled (yes/no)"),
    ("reasoning_effort", "Reasoning effort"),
    ("reasoning_context", "Reasoning context"),
    ("api_key", "API key (blank keeps stored key)"),
)

# Keep the common path short. Less frequently changed limits and reasoning
# controls live behind the review screen instead of making every user tab
# through twelve fields.
PROVIDER_CORE_FIELDS: tuple[tuple[str, str], ...] = (
    PROVIDER_FIELDS[0],
    PROVIDER_FIELDS[2],
    PROVIDER_FIELDS[11],
)
PROVIDER_ADVANCED_FIELDS: tuple[tuple[str, str], ...] = PROVIDER_FIELDS[4:11]


@dataclass(slots=True)
class ProviderWizard:
    """Reusable, storage-free state shared by setup and the in-chat panel."""

    form: ProviderForm
    editing: bool = False
    original_id: str | None = None
    probe_result: ProviderProbeResult | None = None
    connection_verified: bool = False
    model_filter: str = ""

    @classmethod
    def new(cls) -> ProviderWizard:
        return cls(ProviderForm())

    @classmethod
    def edit(cls, view: ProviderView) -> ProviderWizard:
        return cls(ProviderForm.from_view(view), True, view.id)

    def probe_request(self) -> ProviderProbeRequest:
        update = self.form.build_update_for_probe()
        return ProviderProbeRequest(
            provider_id=update.id,
            protocol=update.protocol,
            base_url=update.base_url,
            api_key=update.api_key,
            use_stored_key=self.editing and update.api_key is None,
        )

    def accept_probe(self, result: ProviderProbeResult) -> None:
        self.probe_result = result
        self.connection_verified = result.succeeded
        self.model_filter = ""
        if result.succeeded:
            selected = next((item for item in result.models if item.selectable), None)
            if selected is None:
                self.probe_result = replace(
                    result,
                    succeeded=False,
                    error_kind=ProviderProbeError.NO_MODELS,
                    error_message=(
                        "The provider catalog has no models compatible with "
                        "this protocol."
                    ),
                )
                self.connection_verified = False
                return
            current = next(
                (
                    item
                    for item in result.models
                    if item.id == self.form.model and item.selectable
                ),
                None,
            )
            self.form.model = (current or selected).id

    def use_manual_model(self, model: str) -> None:
        ids = tuple(dict.fromkeys(model.split()))
        if not ids:
            raise ValueError("At least one model ID is required")
        self.form.model = " ".join(ids)
        self.probe_result = None
        self.connection_verified = False

    def filtered_models(self) -> tuple[str, ...]:
        if self.probe_result is None:
            return ()
        needle = self.model_filter.casefold()
        return tuple(
            item.id
            for item in self.probe_result.models
            if item.selectable
            and (
                needle in item.id.casefold()
                or (
                    item.display_name is not None
                    and needle in item.display_name.casefold()
                )
            )
        )

    def build_update(self) -> ProviderUpdate:
        if self.editing and self.original_id != self.form.provider_id.strip():
            raise ValueError("Provider ID cannot be changed while editing")
        update = self.form.build_update()
        if self.probe_result is not None and self.probe_result.succeeded:
            return replace(update, models=())
        return update

    def clear_sensitive(self) -> None:
        self.form.api_key = ""
        self.model_filter = ""
        self.probe_result = None


def _positive(value: str, label: str) -> int | None:
    raw = value.strip()
    if not raw:
        return None
    try:
        number = int(raw)
    except ValueError as error:
        raise ValueError(f"{label} must be a positive integer") from error
    if number < 1:
        raise ValueError(f"{label} must be a positive integer")
    return number


def _number(value: int | None) -> str:
    return "" if value is None else str(value)


__all__ = [
    "PROVIDER_ADVANCED_FIELDS",
    "PROVIDER_CORE_FIELDS",
    "PROVIDER_FIELDS",
    "ProviderForm",
    "ProviderWizard",
]
