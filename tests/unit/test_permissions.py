from pathlib import Path

import pytest

from nano_code.messages import JsonObject
from nano_code.permissions import (
    PermissionBehavior,
    PermissionConfirmation,
    PermissionDecisionKind,
    PermissionDecisionReason,
    PermissionMode,
    PermissionPolicy,
    PermissionRule,
    PermissionUpdate,
    PermissionUpdateDestination,
)
from nano_code.permissions.models import PermissionDecision
from nano_code.permissions.prompt import TerminalPrompter
from nano_code.tools import ToolContext
from nano_code.tools.builtin.bash import BashTool
from nano_code.tools.builtin.read_file import ReadFileTool
from nano_code.tools.builtin.write_file import WriteFileTool


def tool_reason(detail: str = "test") -> PermissionDecisionReason:
    return PermissionDecisionReason(PermissionDecisionKind.TOOL, detail)


@pytest.mark.asyncio
async def test_explicit_deny_precedes_bypass_mode(tmp_path: Path) -> None:
    policy = PermissionPolicy(
        PermissionMode.BYPASS,
        [PermissionRule("Bash", PermissionBehavior.DENY)],
    )

    decision = await policy.decide(
        BashTool(), {"command": "pwd"}, ToolContext(tmp_path)
    )

    assert decision.behavior is PermissionBehavior.DENY


@pytest.mark.asyncio
async def test_explicit_ask_precedes_bypass_mode(tmp_path: Path) -> None:
    policy = PermissionPolicy(
        PermissionMode.BYPASS,
        [PermissionRule("Bash", PermissionBehavior.ASK)],
    )

    decision = await policy.decide(
        BashTool(), {"command": "pwd"}, ToolContext(tmp_path)
    )

    assert decision.behavior is PermissionBehavior.ASK


@pytest.mark.asyncio
async def test_default_allows_reads_and_asks_for_writes(tmp_path: Path) -> None:
    policy = PermissionPolicy()
    (tmp_path / "README.md").write_text("hello", encoding="utf-8")

    read_input: JsonObject = {"path": "README.md"}
    write_input: JsonObject = {"path": "README.md", "content": "replacement"}
    read = await policy.decide(ReadFileTool(), read_input, ToolContext(tmp_path))
    write = await policy.decide(WriteFileTool(), write_input, ToolContext(tmp_path))

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

    read = await policy.decide(
        ReadFileTool(), {"path": "src/readme.txt"}, ToolContext(tmp_path)
    )
    write = await policy.decide(
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

    explicit_ask = await policy.decide(
        WriteFileTool(),
        {"path": "release/build.txt", "content": "x"},
        ToolContext(tmp_path),
    )
    sensitive = await policy.decide(
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

    accepted = await PermissionPolicy(PermissionMode.ACCEPT_EDITS).decide(
        WriteFileTool(), tool_input, ToolContext(tmp_path)
    )
    planned = await PermissionPolicy(PermissionMode.PLAN).decide(
        WriteFileTool(), tool_input, ToolContext(tmp_path)
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

    read = await policy.decide(
        BashTool(), {"command": "git status"}, ToolContext(tmp_path)
    )
    mutation = await policy.decide(
        BashTool(), {"command": "git add ."}, ToolContext(tmp_path)
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

    decision = await policy.decide(
        BashTool(), {"command": "git status"}, ToolContext(tmp_path)
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

    decision = await policy.decide(BashTool(), tool_input, ToolContext(tmp_path))

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

    decision = await policy.decide(
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

    decision = await policy.decide(
        BashTool(),
        {"command": "git status && rm README.md"},
        ToolContext(tmp_path),
    )

    assert decision.behavior is PermissionBehavior.ASK


@pytest.mark.asyncio
async def test_terminal_option_four_creates_bash_session_rule() -> None:
    answers = iter(["4", "git diff:*"])
    prompter = TerminalPrompter(lambda prompt: next(answers))
    decision = PermissionDecision(
        PermissionBehavior.ASK,
        "Allow git diff?",
        tool_reason("bash-approval-required"),
    )

    confirmation = await prompter.confirm(BashTool(), {"command": "git diff"}, decision)

    assert confirmation == PermissionConfirmation(
        True,
        updates=(
            PermissionUpdate.add_rules(
                (
                    PermissionRule(
                        "Bash",
                        PermissionBehavior.ALLOW,
                        "git diff:*",
                        source="localSettings",
                    ),
                ),
                destination=PermissionUpdateDestination.LOCAL,
            ),
        ),
    )


@pytest.mark.asyncio
async def test_terminal_option_four_retries_invalid_prefixes() -> None:
    answers = iter(["4", "", "*", "git diff:*"])
    prompter = TerminalPrompter(lambda prompt: next(answers))
    decision = PermissionDecision(
        PermissionBehavior.ASK,
        "Allow git diff?",
        tool_reason("bash-approval-required"),
    )

    confirmation = await prompter.confirm(BashTool(), {"command": "git diff"}, decision)

    assert confirmation.allowed is True
    assert confirmation.updates[0].rules[0].rule_content == "git diff:*"


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

    confirmation = await prompter.confirm(
        WriteFileTool(), {"path": "a.txt", "content": "x"}, decision
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

    confirmation = await prompter.confirm(
        WriteFileTool(), {"path": "a.txt", "content": "x"}, decision
    )

    assert confirmation == PermissionConfirmation(False)
