from nano_code.agent import ConversationSnapshot
from nano_code.context.microcompact import MicrocompactPolicy
from nano_code.context.planner import ContextPlanner
from nano_code.context.tokenizer import UnicodeTokenEstimator
from nano_code.context.window import ContextWindow
from nano_code.conversation import (
    AssistantMessage,
    HumanMessage,
    ProviderBinding,
    TextContent,
    TokenUsage,
    ToolCall,
    ToolResult,
    ToolResultsMessage,
)
from nano_code.prompts import PromptRegistry, PromptSection, PromptStability
from nano_code.providers.catalog import (
    ActiveModelState,
    CapabilitySource,
    ModelDescriptor,
    ModelLimits,
    resolve_environment,
)


def test_unknown_model_defaults_to_200k_with_180k_auto_compact() -> None:
    environment = resolve_environment(
        ModelDescriptor("unknown", "unknown", source=CapabilitySource.FALLBACK),
        requested_output_tokens=8_192,
        configured_trigger_tokens=None,
    )

    assert environment.descriptor.limits.effective_input_limit(8_192) is None
    assert environment.compact_trigger_tokens == 180_000


def _planner(
    binding: ProviderBinding,
    *,
    trigger: int = 9_000,
    policy: MicrocompactPolicy | None = None,
) -> ContextPlanner:
    descriptor = ModelDescriptor(
        binding.model,
        binding.model,
        ModelLimits(max_input_tokens=10_000),
        source=CapabilitySource.PROFILE_OVERRIDE,
    )
    return ContextPlanner(
        window=ContextWindow(100_000),
        prompt=PromptRegistry(
            (PromptSection("core", PromptStability.STATIC, lambda: "system"),)
        ),
        tools=(),
        max_output_tokens=100,
        microcompact=policy,
        binding_resolver=lambda: binding,
        active_model_state=ActiveModelState(
            resolve_environment(
                descriptor,
                requested_output_tokens=100,
                configured_trigger_tokens=trigger,
            )
        ),
    )


def test_unicode_tokenizer_is_stable_and_not_chars_divided_by_four() -> None:
    estimator = UnicodeTokenEstimator()

    assert estimator.count_text("中文混合测试") == 6
    assert estimator.count_text("中文混合测试") == estimator.count_text("中文混合测试")
    assert estimator.count_text("long_identifier_" * 10) >= 30
    assert estimator.count_text('{"a": [1, 2]}\n😀') > len('{"a": [1, 2]}\n😀') // 4


def test_matching_reported_usage_calibrates_full_request_delta() -> None:
    binding = ProviderBinding("anthropic-messages", "anthropic", "model")
    first = HumanMessage("first")
    anchor = AssistantMessage(
        (TextContent("answer"),),
        TokenUsage(input_tokens=500, output_tokens=20, provider_reported=True),
        parent_uuid=first.uuid,
        provider_binding=binding,
        request_input_tokens_estimate=100,
    )
    latest = HumanMessage("new material", parent_uuid=anchor.uuid)

    plan = _planner(binding).plan(ConversationSnapshot((first, anchor, latest)))

    assert plan.budget is not None
    assert plan.budget.measurement == "reported_calibrated"
    assert plan.budget.input_tokens == (
        500 + plan.request_input_tokens_estimate - 100  # type: ignore[operator]
    )
    assert plan.budget.last_reported_input_tokens == 500


def test_binding_mismatch_uses_local_tokenizer() -> None:
    active = ProviderBinding("openai-responses", "openai", "model-b")
    old = ProviderBinding("openai-responses", "openai", "model-a")
    human = HumanMessage("hello")
    assistant = AssistantMessage(
        (TextContent("answer"),),
        TokenUsage(input_tokens=999, provider_reported=True),
        parent_uuid=human.uuid,
        provider_binding=old,
        request_input_tokens_estimate=10,
    )

    budget = _planner(active).inspect(ConversationSnapshot((human, assistant)))

    assert budget.measurement == "tokenizer_estimate"
    assert budget.last_reported_input_tokens is None


def test_token_trigger_microcompacts_and_retokenizes() -> None:
    binding = ProviderBinding("anthropic-messages", "anthropic", "model")
    human = HumanMessage("read")
    assistant = AssistantMessage(
        (ToolCall("call", "Read", {"path": "x"}),),
        TokenUsage(),
        parent_uuid=human.uuid,
    )
    results = ToolResultsMessage(
        (ToolResult("call", "中" * 500),), assistant.uuid, parent_uuid=assistant.uuid
    )
    policy = MicrocompactPolicy(
        trigger_chars=100_000,
        target_chars=90_000,
        min_result_chars=10,
        keep_recent_results=0,
    )

    plan = _planner(binding, trigger=300, policy=policy).plan(
        ConversationSnapshot((human, assistant, results))
    )

    assert len(plan.new_content_replacements) == 1
    assert plan.budget is not None
    assert plan.budget.input_tokens < 300
