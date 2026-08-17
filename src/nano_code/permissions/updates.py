"""权限更新的验证、持久化和当前 context 应用。"""

from __future__ import annotations

from nano_code.core.paths import SettingsScope
from nano_code.core.settings_store import SettingsStore
from nano_code.permissions.models import (
    PermissionBehavior,
    PermissionRule,
    PermissionUpdate,
    PermissionUpdateDestination,
    PermissionUpdateType,
)
from nano_code.permissions.policy import PermissionPolicy
from nano_code.permissions.rules import permission_rule_to_string

_SCOPE_BY_DESTINATION = {
    PermissionUpdateDestination.USER: SettingsScope.USER,
    PermissionUpdateDestination.PROJECT: SettingsScope.PROJECT,
    PermissionUpdateDestination.LOCAL: SettingsScope.LOCAL,
}


class PermissionUpdateApplier:
    """先持久化再更新内存，避免磁盘失败后产生幽灵授权。"""

    def __init__(
        self,
        policy: PermissionPolicy,
        settings_store: SettingsStore | None = None,
    ) -> None:
        self.policy = policy
        self.settings_store = settings_store

    def apply(self, updates: tuple[PermissionUpdate, ...]) -> None:
        for update in updates:
            self._validate(update)
        for update in updates:
            self._persist(update)
        for update in updates:
            self.policy.apply_update(update)

    def _validate(self, update: PermissionUpdate) -> None:
        if update.type in {
            PermissionUpdateType.ADD_DIRECTORIES,
            PermissionUpdateType.REMOVE_DIRECTORIES,
        }:
            raise ValueError("Additional working directories are not supported yet")
        expected_source = update.destination.value
        for rule in update.rules:
            if rule.source != expected_source:
                raise ValueError(
                    "Permission update rule source must match its destination"
                )
        if (
            update.destination is not PermissionUpdateDestination.SESSION
            and self.settings_store is None
        ):
            raise ValueError("Persistent permission update requires a settings store")

    def _persist(self, update: PermissionUpdate) -> None:
        if update.destination is PermissionUpdateDestination.SESSION:
            return
        assert self.settings_store is not None
        scope = _SCOPE_BY_DESTINATION[update.destination]
        if update.type is PermissionUpdateType.SET_MODE:
            assert update.mode is not None
            self.settings_store.set_permission_mode(scope, update.mode)
            return
        assert update.behavior is not None
        current = self.settings_store.load_scope(scope)
        attribute = f"permission_{update.behavior.value}_rules"
        existing = tuple(getattr(current, attribute))
        rendered = tuple(
            permission_rule_to_string(rule.tool_name, rule.rule_content)
            for rule in update.rules
        )
        if update.type is PermissionUpdateType.ADD_RULES:
            result = tuple(dict.fromkeys((*existing, *rendered)))
        elif update.type is PermissionUpdateType.REPLACE_RULES:
            result = tuple(dict.fromkeys(rendered))
        elif update.type is PermissionUpdateType.REMOVE_RULES:
            removed = set(rendered)
            result = tuple(rule for rule in existing if rule not in removed)
        else:
            raise ValueError(f"Unsupported permission update: {update.type.value}")
        self.settings_store.replace_permission_rules(
            scope, update.behavior.value, result
        )


def permission_rule_for_destination(
    tool_name: str,
    behavior: PermissionBehavior,
    destination: PermissionUpdateDestination,
    rule_content: str | None = None,
) -> PermissionRule:
    return PermissionRule(
        tool_name,
        behavior,
        rule_content,
        source=destination.value,
    )


__all__ = ["PermissionUpdateApplier", "permission_rule_for_destination"]
