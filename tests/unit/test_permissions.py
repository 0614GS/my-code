from pathlib import Path

import pytest

from my_code.foundation.json import JsonObject
from my_code.permissions.models import (
    PermissionBehavior,
    PermissionConfirmation,
    PermissionDecision,
    PermissionDecisionKind,
    PermissionDecisionReason,
    PermissionMode,
    PermissionPrompt,
    PermissionRequest,
    PermissionRule,
    PermissionUpdate,
    PermissionUpdateDestination,
    ToolPermissionContext,
)
from my_code.permissions.policy import PermissionPolicy
from my_code.permissions.prompt import TerminalPrompter
from my_code.tools.base import Tool, ToolContext
from my_code.tools.builtin.bash import BashTool
from my_code.tools.builtin.read_file import ReadFileTool
from my_code.tools.builtin.write_file import WriteFileTool


def tool_reason(detail: str = "test") -> PermissionDecisionReason:
    return PermissionDecisionReason(PermissionDecisionKind.TOOL, detail)


async def decide(
    policy: PermissionPolicy,
    tool: Tool,
    tool_input: JsonObject,
    context: ToolContext,
) -> PermissionDecision:
    local = await tool.check_permissions(
        tool_input,
        ToolPermissionContext(policy.mode, policy.rules, context.cwd),
    )
    return policy.decide(PermissionRequest(tool.definition.name, tool_input, local))


async def confirm(
    prompter: TerminalPrompter,
    tool: Tool,
    tool_input: JsonObject,
    decision: PermissionDecision,
) -> PermissionConfirmation:
    presentation = tool.present_use(tool_input)
    return await prompter.confirm(
        PermissionPrompt(
            tool.definition.name,
            tool_input,
            decision,
            presentation.display_name,
            presentation.summary,
            presentation.activity,
        )
    )


@pytest.mark.asyncio
async def test_explicit_deny_precedes_bypass_mode(tmp_path: Path) -> None:
    policy = PermissionPolicy(
        PermissionMode.BYPASS,
        [PermissionRule("Bash", PermissionBehavior.DENY)],
    )

    decision = await decide(
        policy, BashTool(), {"command": "pwd"}, ToolContext(tmp_path)
    )

    assert decision.behavior is PermissionBehavior.DENY


@pytest.mark.asyncio
async def test_explicit_ask_precedes_bypass_mode(tmp_path: Path) -> None:
    policy = PermissionPolicy(
        PermissionMode.BYPASS,
        [PermissionRule("Bash", PermissionBehavior.ASK)],
    )

    decision = await decide(
        policy, BashTool(), {"command": "pwd"}, ToolContext(tmp_path)
    )

    assert decision.behavior is PermissionBehavior.ASK


@pytest.mark.asyncio
@pytest.mark.parametrize("ask_content", [None, "git:*"])
async def test_local_bash_remembered_allow_overrides_ask_rule(
    tmp_path: Path, ask_content: str | None
) -> None:
    policy = PermissionPolicy(
        rules=[
            PermissionRule(
                "Bash", PermissionBehavior.ASK, ask_content, source="projectSettings"
            ),
            PermissionRule(
                "Bash",
                PermissionBehavior.ALLOW,
                "git push:*",
                source="projectSettings",
            ),
            PermissionRule(
                "Bash",
                PermissionBehavior.ALLOW,
                "git push:*",
                source="localSettings",
            ),
        ]
    )

    decision = await decide(
        policy,
        BashTool(),
        {"command": "git push origin main"},
        ToolContext(tmp_path),
    )

    assert decision.behavior is PermissionBehavior.ALLOW
    assert decision.decision_reason.rule == policy.rules[2]


@pytest.mark.asyncio
async def test_local_bash_remembered_allow_cannot_override_deny_or_plan(
    tmp_path: Path,
) -> None:
    remembered = PermissionRule(
        "Bash", PermissionBehavior.ALLOW, "git push:*", source="localSettings"
    )
    denied = await decide(
        PermissionPolicy(
            rules=[
                PermissionRule("Bash", PermissionBehavior.DENY),
                remembered,
            ]
        ),
        BashTool(),
        {"command": "git push origin main"},
        ToolContext(tmp_path),
    )
    planned = await decide(
        PermissionPolicy(PermissionMode.PLAN, [remembered]),
        BashTool(),
        {"command": "git push origin main"},
        ToolContext(tmp_path),
    )

    assert denied.behavior is PermissionBehavior.DENY
    assert planned.behavior is PermissionBehavior.DENY


@pytest.mark.asyncio
async def test_non_bash_allow_does_not_override_whole_tool_ask(
    tmp_path: Path,
) -> None:
    policy = PermissionPolicy(
        rules=[
            PermissionRule("Write", PermissionBehavior.ASK),
            PermissionRule(
                "Write",
                PermissionBehavior.ALLOW,
                "notes.txt",
                source="localSettings",
            ),
        ]
    )

    decision = await decide(
        policy,
        WriteFileTool(),
        {"path": "notes.txt", "content": "x"},
        ToolContext(tmp_path),
    )

    assert decision.behavior is PermissionBehavior.ASK


@pytest.mark.asyncio
async def test_default_allows_reads_and_asks_for_writes(tmp_path: Path) -> None:
    policy = PermissionPolicy()
    (tmp_path / "README.md").write_text("hello", encoding="utf-8")

    read_input: JsonObject = {"path": "README.md"}
    write_input: JsonObject = {"path": "README.md", "content": "replacement"}
    read = await decide(policy, ReadFileTool(), read_input, ToolContext(tmp_path))
    write = await decide(policy, WriteFileTool(), write_input, ToolContext(tmp_path))

    assert read.behavior is PermissionBehavior.ALLOW
    assert write.behavior is PermissionBehavior.ASK


@pytest.mark.asyncio
async def test_file_content_rules_are_interpreted_by_the_tool(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src/readme.txt").write_text("hello", encoding="utf-8")
    rules = [
        PermissionRule(
            "Read",
            PermissionBehavior.DENY,
            "src/*",
            source="projectSettings",
        ),
        PermissionRule(
            "Write",
            PermissionBehavior.ALLOW,
            "src/*",
            source="localSettings",
        ),
    ]
    policy = PermissionPolicy(rules=rules)

    read = await decide(
        policy, ReadFileTool(), {"path": "src/readme.txt"}, ToolContext(tmp_path)
    )
    write = await decide(
        policy,
        WriteFileTool(),
        {"path": "src/generated.txt", "content": "ok"},
        ToolContext(tmp_path),
    )

    assert read.behavior is PermissionBehavior.DENY
    assert read.decision_reason.rule == rules[0]
    assert write.behavior is PermissionBehavior.ALLOW
    assert write.decision_reason.rule == rules[1]


@pytest.mark.asyncio
async def test_file_ask_and_safety_checks_precede_bypass(tmp_path: Path) -> None:
    policy = PermissionPolicy(
        PermissionMode.BYPASS,
        [
            PermissionRule(
                "Write",
                PermissionBehavior.ASK,
                "release/*",
                source="userSettings",
            )
        ],
    )

    explicit_ask = await decide(
        policy,
        WriteFileTool(),
        {"path": "release/build.txt", "content": "x"},
        ToolContext(tmp_path),
    )
    sensitive = await decide(
        policy,
        WriteFileTool(),
        {"path": ".git/config", "content": "x"},
        ToolContext(tmp_path),
    )

    assert explicit_ask.behavior is PermissionBehavior.ASK
    assert explicit_ask.decision_reason.kind is PermissionDecisionKind.RULE
    assert sensitive.behavior is PermissionBehavior.ASK
    assert sensitive.decision_reason.kind is PermissionDecisionKind.SAFETY


@pytest.mark.asyncio
async def test_write_tool_interprets_accept_edits_and_plan_modes(
    tmp_path: Path,
) -> None:
    tool_input: JsonObject = {"path": "notes.txt", "content": "x"}

    accepted = await decide(
        PermissionPolicy(PermissionMode.ACCEPT_EDITS),
        WriteFileTool(),
        tool_input,
        ToolContext(tmp_path),
    )
    planned = await decide(
        PermissionPolicy(PermissionMode.PLAN),
        WriteFileTool(),
        tool_input,
        ToolContext(tmp_path),
    )

    assert accepted.behavior is PermissionBehavior.ALLOW
    assert accepted.reason == "mode:acceptEdits"
    assert planned.behavior is PermissionBehavior.DENY
    assert planned.reason == "mode:plan"


@pytest.mark.asyncio
async def test_plan_mode_allows_read_only_bash_and_denies_mutation(
    tmp_path: Path,
) -> None:
    policy = PermissionPolicy(PermissionMode.PLAN)

    read = await decide(
        policy, BashTool(), {"command": "git status"}, ToolContext(tmp_path)
    )
    mutation = await decide(
        policy, BashTool(), {"command": "git add ."}, ToolContext(tmp_path)
    )

    assert read.behavior is PermissionBehavior.ALLOW
    assert mutation.behavior is PermissionBehavior.DENY


@pytest.mark.asyncio
async def test_content_deny_rule_precedes_read_only_auto_allow(tmp_path: Path) -> None:
    policy = PermissionPolicy(
        rules=[
            PermissionRule(
                "Bash",
                PermissionBehavior.DENY,
                source="userSettings",
                rule_content="git:*",
            )
        ]
    )

    decision = await decide(
        policy, BashTool(), {"command": "git status"}, ToolContext(tmp_path)
    )

    assert decision.behavior is PermissionBehavior.DENY
    assert decision.reason == "rule:userSettings"


@pytest.mark.asyncio
async def test_content_allow_rule_can_approve_one_mutating_command(
    tmp_path: Path,
) -> None:
    tool_input: JsonObject = {"command": "uv run pytest"}
    policy = PermissionPolicy(
        rules=[
            PermissionRule(
                "Bash",
                PermissionBehavior.ALLOW,
                source="session",
                rule_content="uv run pytest",
            )
        ]
    )

    decision = await decide(policy, BashTool(), tool_input, ToolContext(tmp_path))

    assert decision.behavior is PermissionBehavior.ALLOW
    assert decision.updated_input == tool_input


@pytest.mark.asyncio
async def test_prefix_allow_must_cover_every_compound_subcommand(
    tmp_path: Path,
) -> None:
    policy = PermissionPolicy(
        rules=[
            PermissionRule(
                "Bash",
                PermissionBehavior.ALLOW,
                rule_content="git:*",
            )
        ]
    )

    decision = await decide(
        policy,
        BashTool(),
        {"command": "git status && rm README.md"},
        ToolContext(tmp_path),
    )

    assert decision.behavior is PermissionBehavior.ASK


@pytest.mark.asyncio
async def test_wildcard_allow_must_cover_every_compound_subcommand(
    tmp_path: Path,
) -> None:
    policy = PermissionPolicy(
        rules=[
            PermissionRule(
                "Bash",
                PermissionBehavior.ALLOW,
                rule_content="git status*",
            )
        ]
    )

    decision = await decide(
        policy,
        BashTool(),
        {"command": "git status && rm README.md"},
        ToolContext(tmp_path),
    )

    assert decision.behavior is PermissionBehavior.ASK


@pytest.mark.asyncio
async def test_terminal_bash_option_two_uses_generated_local_rule(
    tmp_path: Path,
) -> None:
    prompts: list[str] = []

    def answer(prompt: str) -> str:
        prompts.append(prompt)
        return "2"

    prompter = TerminalPrompter(answer)
    decision = await decide(
        PermissionPolicy(),
        BashTool(),
        {"command": "git push origin main"},
        ToolContext(tmp_path),
    )

    confirmation = await confirm(
        prompter, BashTool(), {"command": "git push origin main"}, decision
    )

    assert confirmation == PermissionConfirmation(
        True,
        updates=(
            PermissionUpdate.add_rules(
                (
                    PermissionRule(
                        "Bash",
                        PermissionBehavior.ALLOW,
                        "git push:*",
                        source="localSettings",
                    ),
                ),
                destination=PermissionUpdateDestination.LOCAL,
            ),
        ),
    )
    assert "1. Yes" in prompts[0]
    assert '2. Yes, and don\'t ask again for "git push:*"' in prompts[0]
    assert "3. No" in prompts[0]
    assert "4." not in prompts[0]


@pytest.mark.asyncio
async def test_terminal_bash_option_three_denies_without_feedback(
    tmp_path: Path,
) -> None:
    calls = 0

    def answer(prompt: str) -> str:
        nonlocal calls
        del prompt
        calls += 1
        return "3"

    prompter = TerminalPrompter(answer)
    decision = await decide(
        PermissionPolicy(),
        BashTool(),
        {"command": "rm generated.txt"},
        ToolContext(tmp_path),
    )

    confirmation = await confirm(
        prompter, BashTool(), {"command": "rm generated.txt"}, decision
    )

    assert confirmation == PermissionConfirmation(False)
    assert calls == 1


@pytest.mark.asyncio
async def test_terminal_option_four_creates_whole_tool_rule_for_other_tools() -> None:
    answers = iter(["4"])
    prompter = TerminalPrompter(lambda prompt: next(answers))
    decision = PermissionDecision(
        PermissionBehavior.ASK,
        "Allow write?",
        tool_reason("passthrough"),
        suggestions=(
            PermissionUpdate.add_rules(
                (
                    PermissionRule(
                        "Write",
                        PermissionBehavior.ALLOW,
                        "a.txt",
                        source="localSettings",
                    ),
                ),
                destination=PermissionUpdateDestination.LOCAL,
            ),
        ),
    )

    confirmation = await confirm(
        prompter, WriteFileTool(), {"path": "a.txt", "content": "x"}, decision
    )

    assert confirmation == PermissionConfirmation(
        True,
        updates=decision.suggestions,
    )


@pytest.mark.asyncio
async def test_terminal_prompter_fails_closed_on_eof() -> None:
    def raise_eof(prompt: str) -> str:
        del prompt
        raise EOFError

    prompter = TerminalPrompter(raise_eof)
    decision = PermissionDecision(
        PermissionBehavior.ASK,
        "Allow?",
        tool_reason("passthrough"),
    )

    confirmation = await confirm(
        prompter, WriteFileTool(), {"path": "a.txt", "content": "x"}, decision
    )

    assert confirmation == PermissionConfirmation(False)
