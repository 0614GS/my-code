"""State and validation for the native bottom provider editor."""

from __future__ import annotations

from dataclasses import dataclass

from my_code.config.providers import CompactConfig, ProviderProtocol, ReasoningConfig
from my_code.model.capabilities import ModelLimits
from my_code.providers.manager import ProviderUpdate, ProviderView


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
    api_key: str = ""

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
        return ProviderUpdate(
            id=self.provider_id.strip(),
            protocol=ProviderProtocol(protocol_name),
            base_url=self.base_url.strip() or None,
            model=self.model.strip(),
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
        )


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
    PROVIDER_FIELDS[1],
    PROVIDER_FIELDS[2],
    PROVIDER_FIELDS[3],
    PROVIDER_FIELDS[11],
)
PROVIDER_ADVANCED_FIELDS: tuple[tuple[str, str], ...] = PROVIDER_FIELDS[4:11]


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
]
