from nano_code.permissions import (
    PermissionBehavior,
    PermissionMode,
    PermissionPolicy,
    PermissionRule,
)
from nano_code.tools.builtin.bash import BashTool
from nano_code.tools.builtin.read_file import ReadFileTool
from nano_code.tools.builtin.write_file import WriteFileTool


def test_explicit_deny_precedes_bypass_mode() -> None:
    policy = PermissionPolicy(
        PermissionMode.BYPASS,
        [PermissionRule("Bash", PermissionBehavior.DENY)],
    )

    assert policy.decide(BashTool()).behavior is PermissionBehavior.DENY


def test_explicit_ask_precedes_bypass_mode() -> None:
    policy = PermissionPolicy(
        PermissionMode.BYPASS,
        [PermissionRule("Bash", PermissionBehavior.ASK)],
    )

    assert policy.decide(BashTool()).behavior is PermissionBehavior.ASK


def test_default_allows_reads_and_asks_for_writes() -> None:
    policy = PermissionPolicy()

    assert policy.decide(ReadFileTool()).behavior is PermissionBehavior.ALLOW
    assert policy.decide(WriteFileTool()).behavior is PermissionBehavior.ASK


def test_plan_mode_denies_execution() -> None:
    policy = PermissionPolicy(PermissionMode.PLAN)

    assert policy.decide(BashTool()).behavior is PermissionBehavior.DENY
