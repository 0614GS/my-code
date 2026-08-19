"""权限规则字符串的解析、校验与规范化。"""

from __future__ import annotations


def parse_permission_rule(rule: str) -> tuple[str, str | None]:
    """把 ``ToolName`` 或 ``ToolName(content)`` 解析为 (工具名, 内容)。

    ``Tool(*)`` 与 ``Tool()`` 规范化为整工具规则（content 为 ``None``）。
    括号与反斜杠支持转义；``\\*`` 保留给 shell 通配匹配器解释。
    """

    text = rule.strip()
    if not text:
        raise ValueError("Permission rule cannot be blank")

    open_index = _first_unescaped(text, "(")
    if open_index is None:
        _validate_tool_name(text)
        return text, None

    close_index = _last_unescaped(text, ")")
    if close_index is None or close_index <= open_index:
        raise ValueError(f"Malformed permission rule, missing ')' : {rule!r}")
    if close_index != len(text) - 1:
        raise ValueError(f"Malformed permission rule, trailing content: {rule!r}")

    tool_name = text[:open_index]
    _validate_tool_name(tool_name)
    raw_content = text[open_index + 1 : close_index]
    if raw_content == "" or raw_content == "*":
        return tool_name, None
    content = _unescape_rule_content(raw_content)
    if not content.strip():
        raise ValueError(f"Permission rule content cannot be blank: {rule!r}")
    return tool_name, content


def validate_permission_rule(rule: str) -> tuple[str, str | None]:
    """校验通用规则语法；content 的含义由目标工具在运行时解释。"""

    return parse_permission_rule(rule)


def permission_rule_to_string(tool_name: str, rule_content: str | None = None) -> str:
    """输出可再次解析的规范化规则字符串。"""

    if rule_content is None or rule_content == "*" or not rule_content.strip():
        return tool_name
    return f"{tool_name}({_escape_rule_content(rule_content)})"


def validate_bash_rule_content(content: str) -> str:
    """校验交互式 Bash 前缀，拒绝空值与整工具通配。"""

    text = content.strip()
    if not text:
        raise ValueError("Bash rule content cannot be blank")
    # ``Bash(*)`` 会被解析成整工具规则；交互式“不再询问”不接受这种宽泛前缀。
    if text == "*":
        raise ValueError("Bash rule content cannot be a bare wildcard")
    tool_name, parsed_content = validate_permission_rule(f"Bash({text})")
    if parsed_content is None:
        raise ValueError("Bash rule content cannot normalize to the whole tool")
    if tool_name != "Bash":
        raise ValueError("Bash rule content must target Bash")
    return parsed_content


def _validate_tool_name(tool_name: str) -> None:
    if not tool_name.strip() or tool_name != tool_name.strip():
        raise ValueError(f"Permission rule tool name cannot be blank: {tool_name!r}")
    if any(character.isspace() for character in tool_name):
        raise ValueError("Permission rule tool name cannot contain whitespace")


def _first_unescaped(text: str, character: str) -> int | None:
    for index, current in enumerate(text):
        if current == character and not _is_escaped(text, index):
            return index
    return None


def _last_unescaped(text: str, character: str) -> int | None:
    for index in range(len(text) - 1, -1, -1):
        if text[index] == character and not _is_escaped(text, index):
            return index
    return None


def _is_escaped(text: str, index: int) -> bool:
    backslash_count = 0
    cursor = index - 1
    while cursor >= 0 and text[cursor] == "\\":
        backslash_count += 1
        cursor -= 1
    return backslash_count % 2 == 1


def _unescape_rule_content(content: str) -> str:
    result: list[str] = []
    index = 0
    while index < len(content):
        if content[index] == "\\" and index + 1 < len(content):
            following = content[index + 1]
            if following in {"\\", "(", ")"}:
                result.append(following)
                index += 2
                continue
        result.append(content[index])
        index += 1
    return "".join(result)


def _escape_rule_content(content: str) -> str:
    result: list[str] = []
    index = 0
    while index < len(content):
        # 保留 shell 通配转义 ``\*``，避免序列化时退化成未转义通配。
        if content.startswith("\\*", index):
            result.append("\\*")
            index += 2
            continue
        character = content[index]
        if character == "\\":
            result.append("\\\\")
        elif character == "(":
            result.append("\\(")
        elif character == ")":
            result.append("\\)")
        else:
            result.append(character)
        index += 1
    return "".join(result)


__all__ = [
    "parse_permission_rule",
    "permission_rule_to_string",
    "validate_bash_rule_content",
    "validate_permission_rule",
]
