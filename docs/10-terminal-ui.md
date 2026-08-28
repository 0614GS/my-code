# 终端 UI

## 边界

`tui` 是 `mycode` 唯一的聊天 host，使用 `prompt_toolkit + Rich` 负责底部动态布局、按键、slash 命令和事件渲染。它不读取 JSONL、不执行工具、不构造 ModelRequest，也不持有 AgentEngine。主应用使用 `full_screen=False`，不进入 alternate screen、不接管鼠标；已完成内容通过 `run_in_terminal()` 串行固化到终端 scrollback，composer 自然跟随 scrollback，仅在内容占满 terminal 时随原生滚动落到底部。只有临时 transcript pager 使用 alternate buffer。launch 要求 stdin/stdout 都是 TTY；`--session <uuid>` 仍通过同一 TUI 恢复。

模块按职责拆分：`app.py` 只编排 ChatService、生命周期与临时面板转换；`turns.py` 消费前后台 turn 事件；`layout.py` 构造非全屏动态区；`key_bindings.py` 负责按键路由；`composer.py` 适配 slash 与 path 补全；`picker.py` 统一稳定 row key、选择边界和可视窗口；`panels.py` 生成带语义样式的临时面板；`theme.py` 与 `terminal.py` 统一颜色和原生光标策略；`presentation.py` 投影运行时能力；`widgets.py` 生成 Rich scrollback renderable。Provider 表单状态和 resume 摘要分别留在 `provider_screen.py` 与 `resume_screen.py`。

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

text/reasoning 的 delta 只存在于底部动态区；completed 事件到达后清除动态副本并用 Rich 固化。连续工具调用从第一个 started 事件起组成一个有序动态块，完成事件只按 ID 更新原位置，并行完成不重排；块内按相邻类别显示 `Explored`、`Ran commands`、`Changed files` 或 `Called tools`。进入 text、reasoning、Todo、系统消息或回合终态前先移除动态副本，再一次性固化完整工具块；异常、取消、max-steps、后台 continuation 和中断工具遵守相同边界。动态区只展示工具块尾部，scrollback 保留完整块。

Markdown 的流式和最终输出共用 Codex 风格 renderable：标题保留左对齐 `#` 标记，H1 为粗体下划线、H2 为粗体、H3 为粗斜体；行内代码和链接为 cyan，链接带下划线，引用使用绿色 `> `。代码块保留语法高亮但使用 terminal 默认背景且无大块 padding，表格无外框，水平线为短 `———`。所有顶层 scrollback block 之间保持一个空行，Markdown 内部不叠加额外的顶层间距。

## 恢复历史

Chat 从 Session facts 投影 `HistoryEntry`：

- `HistoryText(role, text)` 表示 user、assistant 或 system 文本。
- `HistoryReasoning` 表示已完成 reasoning。
- `HistoryToolCall` 使用执行时保存的 presentation 快照。

TUI 只渲染投影，不重新解释 Conversation 或读取工具结果文件。

成功且实际改变状态的 TodoWrite 在事件位置输出一次 `• Updated Plan` 快照：completed、in-progress、pending 分别使用 `✔`、高亮 `□`、dim `□`；清空列表显示 `(no tasks)`。Todo 不在 composer 上方常驻，成功 TodoWrite 不再重复渲染普通工具结果，失败调用仍保留在工具组。恢复历史从成功 TodoWrite 的安全投影重建同一快照。

## Transcript

`Ctrl+T` 打开独立的全屏 alternate-buffer pager，初始位于持久化会话尾部；↑/↓、PageUp/PageDown、Home/End 导航，`Ctrl+T`、Esc 或 q 返回。pager 不修改主 composer 草稿或原生 scrollback。停留在尾部时会按 revision 跟随新持久化 entry；主动向上滚动后保留当前位置。

完整投影由 `ChatService.current_transcript_view()` 提供不可变判别联合 DTO。顺序和内容来自当前 Session 的本地持久化 conversation：user text、assistant 内容块、允许披露的 reasoning、完整工具参数与持久化结果、conversation summary 和 durable attachment。参数、JSON 与 attachment 使用缩进 key/value 树；普通工具输出按 literal text 展示。投影不包含消息/父节点/工具 UUID、provider binding、token 等调试元数据，不展开外置工具结果文件，也不披露 hidden 或 redacted reasoning；TUI 仍不依赖 Conversation、Session 私有类型或 JSONL codec。

## 权限

TUI 注册 `PermissionHandler`，在底部临时面板展示 `chat.permissions.PermissionRequest` 并返回 `PermissionConfirmation`。Bash 固定显示 Yes、自动生成的 remembered-allow 和 No，默认选中 No，快捷键为 1–3，不进入手工 prefix 或拒绝反馈输入；其他工具保留允许、拒绝、反馈和 suggestion。Provider API key 字段使用密码 processor；无 handler 时权限请求默认拒绝。

普通 composer 空闲或主 Agent 运行时，Shift+Tab 循环 `Ask for me → Approve edits → Full access`。右侧 footer 始终显示友好模式名；Full Access 始终为红色。无 sandbox 时首次进入 Full Access 使用默认 No 的危险确认面板，拒绝或 Esc 保持原模式；确认只在当前进程有效。权限、provider、agent 与危险确认等临时面板打开时不切换模式，危险确认与工具权限请求串行展示。

## Slash 命令

Slash command 在 prompt 进入模型前本地解析。除原有命令外，`/usage`、`/tools`、`/skills [reload]`、`/mcp [refresh|reconnect <server>]`、`/tasks` 和 `/agents` 只调用 ChatService 的窄接口。`/tasks` 同时显示 Bash 与前后台 Subagent，但不提供直接取消；取消仍由 Agent 的 `TaskCancel` 经过权限系统完成。

`/agents` 打开只读 Subagent 选择页，F6 按主会话、活跃 child、最近结束 child 的顺序快速循环。选中后使用与主历史相同的 user、assistant Markdown、reasoning 和配对 tool 组件显示当前进程内完整的安全 transcript，包括初始 prompt、实时文本/reasoning 流、运行中工具、结果与错误；不投影原始工具输入、provider continuation 或隐藏 reasoning 内容。终态 child 最多保留最近 20 个。视图以稳定 task ID 跟踪选择，PageUp/PageDown 浏览，End 回到实时尾部；权限 modal 关闭后恢复原 child 视图，modal 打开时 F6 不切换。Subagent 面板中的 Esc 只回到主会话，不取消 child。

## 输入与临时面板

Enter 提交，Shift+Enter 或 Ctrl+J 换行；传统终端若不能区分 Shift+Enter 与 Enter，应使用 Ctrl+J。逻辑续行使用与 `› ` 等宽的空白前缀，使正文始终对齐。输入 `/` 会立即在 composer 下方展示命令及说明，继续输入时按前缀过滤并默认选中第一项；Enter 直接执行选中的本地命令，Tab 只补全。Slash 候选、命令二级页、permission 和 Full Access 共用 composer 下方的 interaction host；同一时刻只显示最高优先级的交互，path completion 位于其后。候选使用终端默认背景，只通过前景色和粗体标记当前项，并且不绘制内部滚动条。Esc 按候选菜单、临时面板、当前 turn 的顺序处理；主应用与 transcript 只保留 50ms 的原始 VT Escape 序列判定，应用层不再等待后续按键。空闲 Ctrl+C 清空，空输入 Ctrl+D 退出，Ctrl+T 打开 transcript。turn 和后台 continuation 期间 composer 只读，已有草稿保留。

所有选择页使用同一份带稳定 action key 的 rows 驱动渲染、导航和 Enter 动作。默认最多显示七项，长列表随当前项滚动并显示“当前位置/总数”；↑/↓ 在首尾停止，不循环，也不存在不可见但仍可执行的条目。Permission 暂时打断其他面板时会恢复原 row、可视窗口和 composer 草稿。Agent 选择页使用该 interaction host，选中后的完整 child transcript 仍在输入框上方的独立查看区显示。

应用退出时先同步关闭 transcript application，再取消 startup/background watcher，随后擦除底部动态布局并将光标交还 shell；外层 runtime 继续按既有顺序关闭。Welcome、历史、已完成回合和退出提示等 scrollback 内容保留，terminal 中不残留 composer、状态栏或临时面板。

启动期间的 capability 初始化进度只存在于底部动态区，不写入 scrollback；初始化完成后由 Ready 状态取代，不留下过期提示。

流式 Markdown 和 reasoning 从动态预览固化到 scrollback 时，先移除动态投影再输出最终 Rich renderable，禁止最终内容与旧预览在同一帧重复出现并扩大 inline renderer 高度。

TUI 不发送光标形状控制序列，并覆盖 prompt_toolkit 默认的“显示光标时关闭闪烁”行为；VT 重绘只发送光标可见性，因此宽度、形状与闪烁沿用用户终端配置。启动时优先读取终端颜色提示，并在真实 TTY 上用有界 OSC 11 探测背景；用户消息和 composer 使用由背景推导的低对比度表面，探测失败则保持透明。

固化到 scrollback 的用户 prompt 使用一条铺满可用宽度的低对比度背景带和 `›` 标记，AI Markdown 继续使用终端默认背景，因此恢复历史和新回合都能清楚区分双方消息。

resume、provider、permission、Full Access 和 Agent 选择都投影为 composer 下方的临时交互。Resume 和 provider/model 列表不在数据层截断，只由公共可视窗口限制当前绘制行数；resume 标题过长时按显示宽度截断，相对更新时间作为独立尾部字段右对齐。

首次启动缺少有效 Provider 时先运行配置向导，成功保存或选择 profile 后才组装聊天运行时；取消会安全退出且不写半成品。`/provider` 复用同一个 `ProviderWizard` 状态机。新增流程为协议 picker → Provider ID → Base URL → 密码 Key → 自动在线探查 → 可过滤模型 picker → Review；模型目录失败后可以 Retry、编辑 URL/Key 或手工输入模型。模型过滤大小写不敏感，方向键及 PageUp/PageDown 导航；探查中的 Esc 取消网络任务。编辑时 ID 固定，空 Key 保留原值；Save/Cancel 都清除临时 Key、过滤和探查结果并恢复原 composer 草稿。Use 只切换已有 profile，不重复保存。面板仅显示 `stored` 或 `not configured`，本地 Key 存在时提供带二次确认的删除操作。`/auth` 不再存在。

通过 `--session` 启动时，带 `›_  my-code` 紧凑字标的 Welcome 面板后立即绘制完整安全历史；`/clear` 只清理当前可见终端并重绘 Welcome，不修改 canonical conversation。
