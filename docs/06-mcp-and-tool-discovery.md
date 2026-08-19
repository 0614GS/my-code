# MCP 与工具发现

## 当前状态

my-code 当前没有 MCP client、transport、server 配置或 deferred tool discovery。生产包中不预建空 `mcp` 目录，也不把未来能力塞进现有 ToolRegistry。

当前工具来源只有：

- `tools.builtin` 中的本地内置工具。
- `features.todos` 提供的 TodoWrite。

## 未来接入边界

实现 MCP 时应保持以下所有权：

- transport、连接缓存、server 配置解析和远端协议错误归新的 MCP 能力模块。
- 远端 tool schema 转换为 `ModelToolDefinition` 后，才能进入 `ToolRegistry`。
- 调用仍经过 `ToolExecutor` 和 `PermissionPolicy`，远端 server 不能绕过本地权限。
- provider 只看到规范化工具定义，不依赖 MCP SDK 类型。
- Chat 和 Agent 不管理连接，不感知 stdio、HTTP 或其他 transport。

## 生命周期要求

连接缓存需要明确绑定 server 配置，配置改变、transport 关闭或鉴权失败时必须失效。取消信号应贯穿 ToolRound 到远端调用，并仍满足“一次 ToolCall 最终有一个 ToolResult”。

若以后支持 deferred tools，ToolSearch 只负责发现和注册本轮需要的定义；已发生的 ToolCall 和 ToolResult 仍是 Conversation facts，compact 后必须可恢复。

## 实施前提

新增 MCP 前需要先补充：

- 配置 schema 与凭据边界。
- transport 关闭和重连测试。
- 权限拒绝、取消和远端异常回归测试。
- 架构允许表与技术泄漏规则。

在这些决策完成前，不增加 MCP Protocol 或通用 plugin adapter。
