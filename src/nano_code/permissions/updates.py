"""权限更新的验证、持久化和当前 context 应用。"""

from __future__ import annotations

from nano_code.permissions.models import (
    PermissionBehavior,
    PermissionRule,
    PermissionUpdateDestination,
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


__all__ = [
    "permission_rule_for_destination",
]
