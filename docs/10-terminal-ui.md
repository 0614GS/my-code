# 终端 UI

## 参考实现

Claude Code 的界面不是字符串拼接。参考源码使用 React 和 `react-reconciler`，并在 `src/ink/` 中维护定制的 Ink 风格渲染器：Box/Text 组件、Yoga/Flex 布局、焦点和输入事件、ScrollBox、静态消息冻结及 alternate screen。上层再组合 `REPL`、`PromptInput`、`Markdown`、`Spinner`、`Dialog` 和 design system。

nano-code 不复制这套渲染器。在 Python 中使用 Textual 对标“组件树 + 响应式状态 + CSS + 事件循环”，保留设计思想而非机械翻译。

## 组件边界

```text
NanoCodeApp
├── VerticalScroll
│   ├── WelcomePanel
│   ├── UserMessage
│   ├── AssistantMessage (Markdown)
│   ├── ToolCallMessage (in-place status)
│   └── SystemMessage
├── TodoPanel (collapsible, session-scoped projection)
└── composer
    ├── OptionList (slash commands)
    ├── ActivityBar
    ├── PermissionPanel (inline)
    ├── Input
    └── StatusBar

ProviderScreen (modal)
ResumeScreen (modal)
        ↕
ChatRuntime protocol
        ↕
CliChatRuntime ── AgentInboundPort ← AgentEngine
```

TUI 只通过 `ChatRuntime` 的事件流提交输入、读取安全状态、配置无凭据展示 DTO 并注册权限处理器，不接触 Provider SDK、ToolExecutor、SessionStore 或 AgentEngine。CLI composition root 使用 `DeferredPermissionPrompter` 把核心权限询问转换成 UI DTO；无处理器时仍然 fail closed。DTO 携带前端无关的工具 presentation 和 TodoItem，因此以后可以增加普通终端、Web 或测试前端而不修改 Agent。

## 流式显示与持久化边界

Anthropic 适配器把 SSE 文本 delta 转换为 provider-neutral 事件，Agent 再投影成 TUI 事件。增量文本只进入临时 `AssistantMessage`，并以约 30 FPS 的短批次合并后交给 Rich Markdown 渲染；只有收到 SDK 的完整最终 Message 后，Agent 才验证并持久化 assistant message、读取完整工具参数和执行工具。连接中断时屏幕可保留部分文本用于反馈，但 Transcript 不写入半截响应。

工具开始时创建稳定的 `ToolCallMessage`，结束时用相同 tool ID 原位更新。显示名、参数摘要、活动描述和结果预览由对应 Tool 在核心层投影；TUI 只选择颜色、布局和展开方式，不识别 `Read`、`Bash` 等具体工具，也不从原始 tool result 猜测摘要。发送给模型的内容和用户展示内容是独立投影，UI 截断不改变模型上下文。

TodoPanel 的初始值来自 `RuntimeStatus.todos`，resume 使用目标 session 的同一快照；回合
内则消费 `TodoListUpdated`。非空列表首次出现时展开，Ctrl+T 在完整列表与计数/当前项
摘要之间切换，空列表自动隐藏。混合列表保留 completed 项并弱化显示；ActivityBar
优先使用 in-progress 项的 `active_form`。面板有界滚动，长列表不会占满会话区域。

## 权限输入

权限询问使用 composer 内的 `PermissionPanel` 临时替换普通输入框，而不是居中遮罩。选项为 `Yes`、`No`、`No, and tell nano-code why`。第三项打开单行反馈输入；反馈随拒绝的 tool result 返回模型，使下一轮能够调整做法。Esc 和无 UI 环境始终按普通拒绝处理。

## Slash 命令

命令注册表保存名称、别名、说明和本地 action。以 `/` 开头的输入先由注册表处理，未知命令不会发送给模型。OptionList 只消费注册表元数据，支持前缀过滤、方向键选择、Tab 补全和 Enter 执行。

当前 `/clear` 只清理已渲染消息，不删除 Transcript 或运行时上下文。长消息列表虚拟化尚未实现；模型增量、工具状态和权限请求均由 runtime 事件驱动，TUI 不轮询核心对象。

`/provider` 打开独立的 ProviderScreen。URL、模型和 Profile ID 可见；API Key 使用 password Input，空值表示保留旧 Key，ProviderView 只暴露 `has_stored_key` 布尔值。保存后由 runtime 持久化并切换 ProviderRouter，TUI 不直接读写配置文件。环境变量覆盖仍优先于已保存值。

`/resume` 打开独立的 ResumeScreen。列表只展示当前项目的其他可恢复会话：首行是第一次用户输入的归一化预览，次行右侧是相对修改时间；支持方向键、Enter 和 Esc。选择后由 runtime 严格加载目标会话并原子切换 session-scoped 状态，再把历史投影为 `HistoryEntry` 返回 TUI 重建消息组件。工具结果优先复用 Transcript 中的展示快照；旧记录才由 Tool 重新投影。选择器和 TUI 均不读取 JSONL，也不持有核心消息类型。

`/context` 通过 runtime DTO 展示输入 token 估算、输出预留、system/tools/messages
字符组成、工作集消息数和压缩次数，不会触发上下文变更。`/compact` 在普通 activity
状态下执行手动摘要并显示新预算。TUI 不导入 ContextPlanner、CompactBoundary 或
SessionStore；自动 compact 和 reactive compact 同样由 Agent 层决定。

CLI 组合根把 `ProviderRouter`、`ToolRoundExecutor`、`ConversationState`、
`ContextPlanner` 和 `CompactionCoordinator` 装配进 `AgentEngine`。`CliChatRuntime`
只通过 `AgentInboundPort` 的 status/context status、事件和 session 操作生成 DTO；session
切换时由 runtime 在同一锁内切换 Transcript 与工具结果目录。
