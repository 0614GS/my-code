from my_code.context.rebuild import PostCompactContextRebuilder
from my_code.context.session_cache import CompactionInput, ContextPlanningInput
from my_code.conversation.attachments import (
    CollaborationModeAttachment,
    InvokedSkillsAttachment,
    SkillActivationAttachment,
    ToolDiscoveryAttachment,
    ToolDiscoveryDefinition,
    ToolDiscoveryInvalidationAttachment,
)
from my_code.conversation.models import AttachmentMessage


def test_window_only_folds_sorted_skills_and_preserves_tool_routes() -> None:
    old = AttachmentMessage(SkillActivationAttachment("old", "old", "project", "old"))
    current = SkillActivationAttachment("z", "new", "project", "z")
    first = SkillActivationAttachment("a", "a", "project", "a")
    native = ToolDiscoveryDefinition("Native", "native", {}, "new")
    routed = ToolDiscoveryDefinition("Routed", "dispatcher", {}, "one")
    window = (
        AttachmentMessage(InvokedSkillsAttachment((current, first))),
        AttachmentMessage(ToolDiscoveryAttachment((native, routed), "dispatcher")),
        AttachmentMessage(ToolDiscoveryInvalidationAttachment(("Native",))),
        AttachmentMessage(ToolDiscoveryAttachment((native,), "native")),
    )
    state = CompactionInput(
        ContextPlanningInput(window), "session", window[-1].uuid, (old, *window), "plan"
    )
    assert PostCompactContextRebuilder().rebuild(state) == (
        CollaborationModeAttachment("plan"),
        InvokedSkillsAttachment((first, current)),
        ToolDiscoveryAttachment((routed,), "dispatcher"),
        ToolDiscoveryAttachment((native,), "native"),
    )
