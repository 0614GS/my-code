"""Persistence coordination for permission updates."""

from nano_code.config.paths import SettingsScope
from nano_code.config.store import SettingsStore
from nano_code.permissions import (
    PermissionPolicy,
    PermissionUpdate,
    PermissionUpdateDestination,
    PermissionUpdateType,
    permission_rule_to_string,
)

_SCOPE_BY_DESTINATION = {
    PermissionUpdateDestination.USER: SettingsScope.USER,
    PermissionUpdateDestination.PROJECT: SettingsScope.PROJECT,
    PermissionUpdateDestination.LOCAL: SettingsScope.LOCAL,
}


class PermissionUpdateApplier:
    """Persist every update before changing the live permission policy."""

    def __init__(
        self, policy: PermissionPolicy, settings_store: SettingsStore | None = None
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
        if any(rule.source != expected_source for rule in update.rules):
            raise ValueError("Permission update rule source must match its destination")
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


__all__ = ["PermissionUpdateApplier"]
