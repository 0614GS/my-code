import json
from dataclasses import replace
from pathlib import Path

import pytest

from my_code.context.meter import DOCUMENT_TOKENS, IMAGE_TOKENS, ContextMeter
from my_code.context.planner import ContextPlanner
from my_code.context.session_cache import ContextPlanningInput, SessionContextCache
from my_code.conversation.models import AssistantMessage, HumanMessage, TextContent
from my_code.model.primitives import ContextFootprint, ProviderBinding, TokenUsage
from my_code.model.request import (
    AssistantOutput,
    InputDocument,
    InputImage,
    InputText,
    ModelRequest,
    ModelTextBlock,
    PromptStability,
    SystemPrompt,
    UserInput,
)
from my_code.prompts.models import PromptSection
from my_code.prompts.registry import PromptRegistry


def _binding(provider: str = "provider", model: str = "model") -> ProviderBinding:
    return ProviderBinding("test", provider, model)


def _request(*blocks) -> ModelRequest:  # type: ignore[no-untyped-def]
    return ModelRequest(
        SystemPrompt.from_text("system"),
        (UserInput(tuple(blocks)),),
        (),
        100,
    )


def test_first_request_uses_four_chars_per_token_and_persists_ratio(
    tmp_path: Path,
) -> None:
    cache = tmp_path / ".token-estimates.json"
    meter = ContextMeter(cache_path=cache)
    footprint = meter.footprint(_request(InputText("hello")))

    estimate = meter.estimate(_binding(), footprint)
    assert estimate.tokens == (len(footprint.text) + 3) // 4
    assert estimate.source == "four_chars_per_token"

    meter.calibrate(
        _binding(),
        footprint,
        TokenUsage(37, 2, provider_reported=True),
    )
    assert cache.stat().st_mode & 0o777 == 0o600
    assert json.loads(cache.read_text()) == {
        "provider:model": [len(footprint.text), 37]
    }

    restarted = ContextMeter(cache_path=cache)
    calibrated = restarted.estimate(_binding(), ContextFootprint("x" * 10))
    assert calibrated.tokens == (10 * 37 + len(footprint.text) - 1) // len(
        footprint.text
    )
    assert calibrated.source == "calibrated_ratio"


def test_ratio_is_first_writer_wins_and_binding_isolated(tmp_path: Path) -> None:
    cache = tmp_path / "ratios.json"
    meter = ContextMeter(cache_path=cache)
    footprint = ContextFootprint("x" * 40)
    meter.calibrate(_binding(), footprint, TokenUsage(20, 1, provider_reported=True))
    meter.calibrate(_binding(), footprint, TokenUsage(99, 1, provider_reported=True))

    assert meter.estimate(_binding(), footprint).tokens == 20
    assert meter.estimate(_binding(model="other"), footprint).tokens == 10
    assert meter.estimate(_binding(provider="other"), footprint).tokens == 10


def test_token_counter_takes_precedence_over_ratio_fallback(tmp_path: Path) -> None:
    class Counter:
        def count(self, model: str, text: str) -> int | None:
            assert model == "model"
            assert text == "content"
            return 7

    meter = ContextMeter(counter=Counter(), cache_path=tmp_path / "ratios.json")
    estimate = meter.estimate(_binding(), ContextFootprint("content"))
    assert estimate.tokens == 7
    assert estimate.source == "token_counter"


def test_media_uses_fixed_allowances_and_does_not_calibrate(tmp_path: Path) -> None:
    cache = tmp_path / "ratios.json"
    meter = ContextMeter(cache_path=cache)
    footprint = meter.footprint(
        _request(
            InputImage("image/png", "base64-data"),
            InputDocument("application/pdf", "base64-document"),
        )
    )
    assert "base64-data" not in footprint.text
    assert "base64-document" not in footprint.text
    assert meter.estimate(_binding(), footprint).tokens >= (
        IMAGE_TOKENS + DOCUMENT_TOKENS
    )

    meter.calibrate(_binding(), footprint, TokenUsage(999, 1, provider_reported=True))
    assert not cache.exists()


def test_corrupt_cache_warns_and_falls_back(tmp_path: Path) -> None:
    cache = tmp_path / "ratios.json"
    cache.write_text("not-json")
    with pytest.warns(RuntimeWarning, match="corrupt token estimate cache"):
        meter = ContextMeter(cache_path=cache)
    estimate = meter.estimate(_binding(), ContextFootprint("x" * 9))
    assert estimate.tokens == 3


def test_reported_anchor_includes_output_once_and_delta_only(tmp_path: Path) -> None:
    binding = _binding()
    meter = ContextMeter(cache_path=tmp_path / "ratios.json")
    prompt = PromptRegistry(
        (PromptSection("core", PromptStability.STATIC, lambda: "system"),)
    )
    planner = ContextPlanner(
        prompt=prompt,
        max_output_tokens=100,
        binding_resolver=lambda: binding,
        meter=meter,
    )
    first = HumanMessage("first")
    first_plan = planner.plan(
        ContextPlanningInput((first,)), SessionContextCache(), tools=()
    )
    assert first_plan.budget is not None
    assert first_plan.budget.cache_hit_rate is None
    assert first_plan.request_footprint is not None
    response = AssistantMessage(
        (TextContent("answer"),),
        TokenUsage(60, 20, 20, 20, True),
        parent_uuid=first.uuid,
        provider_binding=binding,
        context_footprint=meter.response_footprint(
            first_plan.request,
            AssistantOutput((ModelTextBlock("answer"),)),
        ),
    )

    anchored = planner.plan(
        ContextPlanningInput((first, response)), SessionContextCache(), tools=()
    )
    assert anchored.budget is not None
    assert anchored.budget.reported_base_tokens == 120
    assert anchored.budget.estimated_delta_tokens == 0
    assert anchored.budget.projected_tokens == 120
    assert anchored.budget.measurement == "reported"
    assert anchored.budget.cache_hit_rate == pytest.approx(0.2)

    zero_usage = planner.plan(
        ContextPlanningInput(
            (first, replace(response, usage=TokenUsage(provider_reported=True)))
        ),
        SessionContextCache(),
        tools=(),
    )
    assert zero_usage.budget is not None
    assert zero_usage.budget.cache_hit_rate == 0.0

    other_binding = ProviderBinding(
        binding.protocol, binding.provider_id, "other-model", binding.base_url
    )
    mismatched = planner.plan(
        ContextPlanningInput(
            (first, replace(response, provider_binding=other_binding))
        ),
        SessionContextCache(),
        tools=(),
    )
    assert mismatched.budget is not None
    assert mismatched.budget.cache_hit_rate is None

    next_user = HumanMessage("more", parent_uuid=response.uuid)
    grown = planner.plan(
        ContextPlanningInput((first, response, next_user)),
        SessionContextCache(),
        tools=(),
    )
    assert grown.budget is not None
    assert grown.budget.projected_tokens > 120
