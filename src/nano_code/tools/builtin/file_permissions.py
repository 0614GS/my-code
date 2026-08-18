"""工作区文件工具共享的输入级权限判断。"""

from __future__ import annotations

import re
from pathlib import Path

from nano_code.conversation import JsonObject
from nano_code.permissions.models import (
    PermissionBehavior,
    PermissionDecisionKind,
    PermissionDecisionReason,
    PermissionMode,
    PermissionRule,
    PermissionUpdate,
    PermissionUpdateDestination,
    ToolPermissionContext,
    ToolPermissionResult,
)
from nano_code.tools.base import ToolInputError
from nano_code.tools.paths import (
    is_sensitive_write_path,
    resolve_workspace_path,
)


def check_read_permission(
    tool_name: str,
    tool_input: JsonObject,
    context: ToolPermissionContext,
    *,
    path_key: str = "path",
    must_exist: bool = True,
) -> ToolPermissionResult:
    prepared = _prepare_path_input(tool_input, context, path_key, must_exist=must_exist)
    if isinstance(prepared, ToolPermissionResult):
        return prepared
    updated_input, path, rule_path = prepared

    matched = _matching_rule(context, tool_name, PermissionBehavior.DENY, rule_path)
    if matched is not None:
        return _rule_deny(matched, f"Reading {rule_path} is denied by rule.")
    matched = _matching_rule(context, tool_name, PermissionBehavior.ASK, rule_path)
    if matched is not None:
        return _rule_ask(
            matched,
            f"Reading {rule_path} requires confirmation by rule.",
            updated_input,
        )
    matched = _matching_rule(context, tool_name, PermissionBehavior.ALLOW, rule_path)
    if matched is not None:
        return _rule_allow(
            matched, updated_input, f"Reading {rule_path} is allowed by rule."
        )

    del path
    return ToolPermissionResult.allow(
        updated_input,
        message="Workspace read is allowed.",
        reason=_tool_reason("workspace-read"),
    )


def check_write_permission(
    tool_name: str,
    tool_input: JsonObject,
    context: ToolPermissionContext,
    *,
    must_exist: bool,
) -> ToolPermissionResult:
    prepared = _prepare_path_input(tool_input, context, "path", must_exist=must_exist)
    if isinstance(prepared, ToolPermissionResult):
        return prepared
    updated_input, path, rule_path = prepared

    matched = _matching_rule(context, tool_name, PermissionBehavior.DENY, rule_path)
    if matched is not None:
        return _rule_deny(matched, f"Writing {rule_path} is denied by rule.")
    matched = _matching_rule(context, tool_name, PermissionBehavior.ASK, rule_path)
    if matched is not None:
        return _rule_ask(
            matched,
            f"Writing {rule_path} requires confirmation by rule.",
            updated_input,
        )

    if is_sensitive_write_path(context.tool_context.cwd, path):
        return ToolPermissionResult.ask(
            message=f"Writing sensitive path {rule_path} requires explicit approval.",
            reason=PermissionDecisionReason(
                PermissionDecisionKind.SAFETY, "sensitive-workspace-path"
            ),
            bypass_immune=True,
            updated_input=updated_input,
        )

    if context.mode is PermissionMode.PLAN:
        return ToolPermissionResult.deny(
            message=f"{tool_name} is unavailable in plan mode.",
            reason=PermissionDecisionReason(PermissionDecisionKind.MODE, "plan"),
        )

    if context.mode is PermissionMode.ACCEPT_EDITS:
        return ToolPermissionResult.allow(
            updated_input,
            message="Workspace edit is allowed by acceptEdits mode.",
            reason=PermissionDecisionReason(PermissionDecisionKind.MODE, "acceptEdits"),
        )

    matched = _matching_rule(context, tool_name, PermissionBehavior.ALLOW, rule_path)
    if matched is not None:
        return _rule_allow(
            matched, updated_input, f"Writing {rule_path} is allowed by rule."
        )

    suggestion_rule = PermissionRule(
        tool_name,
        PermissionBehavior.ALLOW,
        rule_path,
        source=PermissionUpdateDestination.LOCAL.value,
    )
    return ToolPermissionResult.passthrough(
        message=f"Allow {tool_name} to write {rule_path}?",
        reason=_tool_reason("workspace-write"),
        updated_input=updated_input,
        suggestions=(
            PermissionUpdate.add_rules(
                (suggestion_rule,), destination=PermissionUpdateDestination.LOCAL
            ),
        ),
    )


def _prepare_path_input(
    tool_input: JsonObject,
    context: ToolPermissionContext,
    path_key: str,
    *,
    must_exist: bool,
) -> tuple[JsonObject, Path, str] | ToolPermissionResult:
    raw_path = tool_input.get(path_key, ".")
    if not isinstance(raw_path, str) or not raw_path:
        return ToolPermissionResult.deny(
            message=f"Invalid {path_key} input.",
            reason=_tool_reason("invalid-path"),
        )
    try:
        path = resolve_workspace_path(
            context.tool_context.cwd,
            raw_path,
            must_exist=must_exist,
        )
    except (ToolInputError, OSError) as error:
        return ToolPermissionResult.deny(
            message=str(error),
            reason=PermissionDecisionReason(
                PermissionDecisionKind.SAFETY, "workspace-boundary"
            ),
        )
    rule_path = path.relative_to(context.tool_context.cwd.resolve()).as_posix()
    updated_input = dict(tool_input)
    updated_input[path_key] = rule_path or "."
    return updated_input, path, rule_path or "."


def _matching_rule(
    context: ToolPermissionContext,
    tool_name: str,
    behavior: PermissionBehavior,
    path: str,
) -> PermissionRule | None:
    candidates = (path, f"./{path}") if path != "." else (".", "./")
    for rule in context.rules_for(tool_name, behavior):
        assert rule.rule_content is not None
        if any(
            _wildcard_matches(rule.rule_content, candidate) for candidate in candidates
        ):
            return rule
    return None


def _wildcard_matches(pattern: str, value: str) -> bool:
    pattern = pattern.strip().replace("\\/", "/")
    parts: list[str] = []
    index = 0
    has_wildcard = False
    while index < len(pattern):
        if pattern[index] == "\\" and index + 1 < len(pattern):
            following = pattern[index + 1]
            if following in {"*", "\\"}:
                parts.append(re.escape(following))
                index += 2
                continue
        if pattern[index] == "*":
            parts.append(".*")
            has_wildcard = True
        else:
            parts.append(re.escape(pattern[index]))
        index += 1
    regex = "".join(parts)
    return (
        re.fullmatch(regex, value, flags=re.DOTALL) is not None
        if has_wildcard
        else value == _literal(pattern)
    )


def _literal(pattern: str) -> str:
    return pattern.replace(r"\*", "*").replace(r"\\", "\\")


def _rule_reason(rule: PermissionRule) -> PermissionDecisionReason:
    return PermissionDecisionReason(
        PermissionDecisionKind.RULE,
        f"{rule.tool_name}:{rule.behavior.value}",
        rule=rule,
    )


def _tool_reason(detail: str) -> PermissionDecisionReason:
    return PermissionDecisionReason(PermissionDecisionKind.TOOL, detail)


def _rule_deny(rule: PermissionRule, message: str) -> ToolPermissionResult:
    return ToolPermissionResult.deny(message=message, reason=_rule_reason(rule))


def _rule_ask(
    rule: PermissionRule, message: str, updated_input: JsonObject
) -> ToolPermissionResult:
    return ToolPermissionResult.ask(
        message=message,
        reason=_rule_reason(rule),
        bypass_immune=True,
        updated_input=updated_input,
    )


def _rule_allow(
    rule: PermissionRule, updated_input: JsonObject, message: str
) -> ToolPermissionResult:
    return ToolPermissionResult.allow(
        updated_input,
        message=message,
        reason=_rule_reason(rule),
    )


__all__ = ["check_read_permission", "check_write_permission"]
