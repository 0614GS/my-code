# 终端 UI

## 边界

`tui` 是 `mycode` 唯一的聊天 host，使用 `prompt_toolkit + Rich` 负责底部动态布局、按键、slash 命令和事件渲染。它不读取 JSONL、不执行工具、不构造 ModelRequest，也不持有 AgentEngine。应用使用 `full_screen=False`，不进入 alternate screen、不接管鼠标；已完成内容通过 `run_in_terminal()` 串行固化到终端 scrollback。launch 要求 stdin/stdout 都是 TTY；`--session <uuid>` 仍通过同一 TUI 恢复。

模块按职责拆分：`app.py` 只编排 ChatService、生命周期与临时面板状态；`turns.py` 消费前后台 turn 事件；`layout.py` 构造非全屏动态区；`key_bindings.py` 负责按键路由；`composer.py` 持有独立 slash 选择状态并适配 path 补全；`panels.py` 生成带语义样式的临时面板；`theme.py` 与 `terminal.py` 统一颜色和原生光标策略；`presentation.py` 投影运行时能力；`widgets.py` 生成 Rich scrollback renderable。Provider 表单状态和 resume 摘要分别留在 `provider_screen.py` 与 `resume_screen.py`。

状态栏只渲染最近一次安全的 context 快照，并在启动、回合结束、会话恢复或配置变更等稳定边界刷新。普通状态与 context 状态分别隔离刷新；刷新失败时保留最近有效值并显示短警告，异常不会逃逸到 prompt-toolkit event loop。prompt-toolkit 的重绘回调不会在工具调用等待结果的中间态重新执行 context 规划。

TUI 通过语义子模块直接使用能力的真实所有者：

- `chat.service`：用户级用例。
- `chat.events`、`chat.history`、`chat.status`、`chat.views`、`chat.permissions`：Chat 自有安全 DTO。
- `providers.manager` 与 `config.providers`：provider 管理表单。
- `permissions.models/rules/updates`：权限确认控件。
- `sessions.catalog`：恢复列表的安全摘要。
- `tools.presentation`、`model.primitives` 和 feature models：展示值。

TUI 只通过 `ChatService` 获取 session history、usage、工具、Skill、MCP 与 owner 隔离的后台任务投影，不直接访问 Session、ToolCatalog、McpRuntime、SkillRuntime、TaskSupervisor 或后台 registry。TUI 仍不依赖 Provider SDK、SessionStore、ToolExecutor、Conversation 或 Context。

## 事件流

`MyCodeTui` 接收已组装的 `ChatService`。启动先调用 `initialize()`，完成 MCP/Skill 可选能力初始化并取得首页与恢复历史。一次 prompt 使用 `ChatService.stream()`，按事件更新：

- text 与 reasoning 的 started/delta/completed。
- attachment loaded。
- tool started/finished。
- todo list updated。
- `TurnSucceeded` 或 `MaxStepsReached` 终态。

text/reasoning 的 delta 只存在于底部动态区；completed 事件到达后清除动态副本并用 Rich 固化。工具完成、每回合 steps 与 token usage、max-steps、异常和中断各自固化成独立行。已固化内容不原地修改。

## 恢复历史

Chat 从 Session facts 投影 `HistoryEntry`：

- `HistoryText(role, text)` 表示 user、assistant 或 system 文本。
- `HistoryReasoning` 表示已完成 reasoning。
- `HistoryToolCall` 使用执行时保存的 presentation 快照。

TUI 只渲染投影，不重新解释 Conversation 或读取工具结果文件。

## 权限

TUI 注册 `PermissionHandler`，在底部临时面板展示 `chat.permissions.PermissionRequest` 并返回 `PermissionConfirmation`。Bash 固定显示 Yes、自动生成的 remembered-allow 和 No，默认选中 No，快捷键为 1–3，不进入手工 prefix 或拒绝反馈输入；其他工具保留允许、拒绝、反馈和 suggestion。Provider API key 字段使用密码 processor；无 handler 时权限请求默认拒绝。

普通 composer 空闲或主 Agent 运行时，Shift+Tab 循环 `Ask for me → Approve edits → Full access`。右侧 footer 始终显示友好模式名；Full Access 始终为红色。无 sandbox 时首次进入 Full Access 使用默认 No 的危险确认面板，拒绝或 Esc 保持原模式；确认只在当前进程有效。权限、provider、agent 与危险确认等临时面板打开时不切换模式，危险确认与工具权限请求串行展示。

## Slash 命令

Slash command 在 prompt 进入模型前本地解析。除原有命令外，`/usage`、`/tools`、`/skills [reload]`、`/mcp [refresh|reconnect <server>]`、`/tasks` 和 `/agents` 只调用 ChatService 的窄接口。`/tasks` 同时显示 Bash 与前后台 Subagent，但不提供直接取消；取消仍由 Agent 的 `TaskCancel` 经过权限系统完成。

`/agents` 打开只读 Subagent 选择页，F6 按主会话、活跃 child、最近结束 child 的顺序快速循环。选中后使用与主历史相同的 user、assistant Markdown、reasoning 和配对 tool 组件显示当前进程内完整的安全 transcript，包括初始 prompt、实时文本/reasoning 流、运行中工具、结果与错误；不投影原始工具输入、provider continuation 或隐藏 reasoning 内容。终态 child 最多保留最近 20 个。视图以稳定 task ID 跟踪选择，PageUp/PageDown 浏览，End 回到实时尾部；权限 modal 关闭后恢复原 child 视图，modal 打开时 F6 不切换。Subagent 面板中的 Esc 只回到主会话，不取消 child。

## 输入与临时面板

Enter 提交，Alt+Enter（或 Esc 后 Enter）换行。输入 `/` 会立即在 composer 下方展示命令及说明，继续输入时按前缀过滤并默认选中第一项；Enter 直接执行选中的本地命令，Tab 只补全。候选使用终端默认背景，只通过前景色和粗体标记当前项，并且不绘制内部滚动条。Esc 按候选菜单、临时面板、当前 turn 的顺序处理；空闲 Ctrl+C 清空，空输入 Ctrl+D 退出，Ctrl+T 切换 Todo。turn 和后台 continuation 期间 composer 只读，已有草稿保留。

TUI 不发送光标形状控制序列，并覆盖 prompt_toolkit 默认的“显示光标时关闭闪烁”行为；VT 重绘只发送光标可见性，因此宽度、形状与闪烁沿用用户终端配置。启动时优先读取终端颜色提示，并在真实 TTY 上用有界 OSC 11 探测背景；用户消息和 composer 使用由背景推导的低对比度表面，探测失败则保持透明。

固化到 scrollback 的用户 prompt 使用一条铺满可用宽度的低对比度背景带和 `›` 标记，AI Markdown 继续使用终端默认背景，因此恢复历史和新回合都能清楚区分双方消息。

resume、provider 和 permission 都投影为底部临时面板。Provider 采用与参考实现相同的“列表选择、方向键导航、Enter 执行主操作、Esc 返回”交互语法：profile 选择后先提供 Use/Configure，配置默认只经过五项核心字段，在 Review 页保存、发现模型或按需进入 Advanced；面板显示不含密钥的凭据来源，本地 Key 存在时提供带二次确认的删除操作。删除不会影响环境 Key；删除当前 Provider 的本地 Key 后连接立即重新解析。`/auth` 不再存在。通过 `--session` 启动时，带 my-code ASCII 标识的 Welcome 面板后立即绘制完整安全历史；`/clear` 只清理当前可见终端并重绘 Welcome，不修改 canonical conversation。
