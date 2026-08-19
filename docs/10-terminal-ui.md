# 终端 UI

## 边界

`tui` 是 Textual host，只负责布局、组件、按键、slash 命令和事件渲染。它不读取 JSONL、不执行工具、不构造 ModelRequest，也不持有 AgentEngine。

TUI 通过语义子模块直接使用能力的真实所有者：

- `chat.service`：用户级用例。
- `chat.events`、`chat.history`、`chat.status`、`chat.permissions`：Chat 自有 DTO。
- `providers.manager` 与 `config.providers`：provider 管理表单。
- `permissions.models/rules/updates`：权限确认控件。
- `sessions.catalog`：恢复列表的安全摘要。
- `tools.presentation`、`model.primitives` 和 feature models：展示值。

Chat 不再把这些外部类型二次导出给 TUI。依赖增加是显式所有权，而不是技术泄漏；TUI 仍不依赖 Provider SDK、SessionStore、ToolExecutor、Conversation 或 Context。

## 事件流

`NanoCodeTui` 接收已组装的 `ChatService`。一次 prompt 使用 `ChatService.stream()`，按事件更新：

- text 与 reasoning 的 started/delta/completed。
- attachment loaded。
- tool started/finished。
- todo list updated。
- `TurnSucceeded` 或 `MaxStepsReached` 终态。

终态值本身就是最后一个 stream event，不再套一层只有 `result` 字段的 dataclass。

## 恢复历史

Chat 从 Session facts 投影 `HistoryEntry`：

- `HistoryText(role, text)` 表示 user、assistant 或 system 文本。
- `HistoryReasoning` 表示已完成 reasoning。
- `HistoryToolCall` 使用执行时保存的 presentation 快照。

TUI 只渲染投影，不重新解释 Conversation 或读取工具结果文件。

## 权限

TUI 注册 `PermissionHandler`，展示 `chat.permissions.PermissionRequest` 并返回 `PermissionConfirmation`。密码和 API key 输入使用安全控件；无 handler 时权限请求默认拒绝。

## Slash 命令

Slash command 在 prompt 进入模型前本地解析。status、context、compact、provider、resume、clear 和 exit 分别调用 ChatService 或操作 UI，不伪装成用户消息。
