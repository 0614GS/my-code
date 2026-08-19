from pathlib import Path

import pytest

from my_code.config.paths import MyCodePaths, SettingsScope
from my_code.config.permission_updates import PermissionUpdateApplier
from my_code.config.store import SettingsFileError, SettingsStore
from my_code.permissions.models import (
    PermissionBehavior,
    PermissionMode,
    PermissionRule,
    PermissionUpdate,
    PermissionUpdateDestination,
    PermissionUpdateType,
)
from my_code.permissions.policy import PermissionPolicy


class FailingSettingsStore(SettingsStore):
    def replace_permission_rules(
        self,
        scope: SettingsScope,
        behavior: str,
        rules: tuple[str, ...],
    ) -> None:
        del scope, behavior, rules
        raise SettingsFileError("simulated persistence failure")


def test_persistence_failure_does_not_update_in_memory_policy(tmp_path: Path) -> None:
    policy = PermissionPolicy()
    store = FailingSettingsStore(MyCodePaths(tmp_path, tmp_path / "config"))
    rule = PermissionRule(
        "Write",
        PermissionBehavior.ALLOW,
        "notes.txt",
        source="localSettings",
    )
    update = PermissionUpdate.add_rules(
        (rule,), destination=PermissionUpdateDestination.LOCAL
    )

    with pytest.raises(SettingsFileError, match="simulated"):
        PermissionUpdateApplier(policy, store).apply((update,))

    assert policy.rules == ()


def test_replace_rules_only_replaces_the_selected_source() -> None:
    user_rule = PermissionRule("Read", PermissionBehavior.ALLOW, source="userSettings")
    old_session = PermissionRule("Write", PermissionBehavior.ALLOW, source="session")
    new_session = PermissionRule(
        "Write",
        PermissionBehavior.ALLOW,
        "notes.txt",
        source="session",
    )
    policy = PermissionPolicy(rules=(user_rule, old_session))
    update = PermissionUpdate(
        PermissionUpdateType.REPLACE_RULES,
        PermissionUpdateDestination.SESSION,
        rules=(new_session,),
        behavior=PermissionBehavior.ALLOW,
    )

    PermissionUpdateApplier(policy).apply((update,))

    # Replacing session rules must not remove rules from persistent sources.
    assert user_rule in policy.rules
    assert old_session not in policy.rules
    assert new_session in policy.rules


def test_session_mode_update_changes_current_policy() -> None:
    policy = PermissionPolicy()
    update = PermissionUpdate(
        PermissionUpdateType.SET_MODE,
        PermissionUpdateDestination.SESSION,
        mode=PermissionMode.ACCEPT_EDITS,
    )

    PermissionUpdateApplier(policy).apply((update,))

    assert policy.mode is PermissionMode.ACCEPT_EDITS


def test_additional_directories_are_reserved_but_not_enabled() -> None:
    update = PermissionUpdate(
        PermissionUpdateType.ADD_DIRECTORIES,
        PermissionUpdateDestination.SESSION,
        directories=("../other",),
    )

    with pytest.raises(ValueError, match="not supported"):
        PermissionUpdateApplier(PermissionPolicy()).apply((update,))
