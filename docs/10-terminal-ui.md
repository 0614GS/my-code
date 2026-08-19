# 终端 UI

> 本文件的 UI 行为仍有效；旧 `application.chat.ChatRuntime`、`DefaultChatRuntime`、Agent inbound port 与 `core.bootstrap` 结构已被顶层具体 `ChatService` 和根 `bootstrap.py` 取代，见 `refactor/15-phase-6-7-finalization.md`。

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
application.chat.ChatRuntime protocol
        ↕
DefaultChatRuntime ── AgentInboundPort ← AgentEngine
```

TUI 只通过 `application.chat.ChatRuntime` 的事件流提交输入、读取安全状态、配置无凭据展示 DTO 并注册权限处理器，不接触 Provider SDK、ToolExecutor、SessionStore 或 AgentEngine。`core.bootstrap` 组装完整应用，Application runtime 使用 `DeferredPermissionPrompter` 把核心权限询问转换成 UI DTO；无处理器时仍然 fail closed。DTO 携带前端无关的工具 presentation 和 TodoItem，因此以后可以增加普通终端、Web 或测试前端而不修改 Agent。

正常回答和显式 Step 上限是两种独立终态。TUI 收到 max-steps 终态后显示明确错误、
结束 busy 状态并重新启用输入框，用户可以基于已经提交的工具结果继续对话。

## 流式显示与持久化边界

Provider adapter 把文本与 reasoning 转换为 started/delta/completed 中立生命周期，并把
可能交错的 provider output item 顺序化为单活动展示块。增量只进入临时组件；completed
携带最终正文或 presentation，并以该快照原子收口临时累计内容。最终 `ModelOutput` 不再
驱动第二次展示，因此无需在 UI 事件中携带 reasoning UUID。

`ReasoningMessage` 对实时内容默认展开，完成后可折叠，resume 历史默认折叠。
verbatim/summary 使用不同标题，redacted/hidden 只显示安全占位。错误或取消只把当前
未闭合组件标记中断，已经完成的前序 reasoning 保持正常；半截响应不写 Transcript。
只有收到完整最终响应后才持久化，`-p` 仍只输出最终正文。

工具开始时创建稳定的 `ToolCallMessage`，结束时用相同 tool ID 原位更新。显示名、参数摘要、活动描述和结果预览由对应 Tool 在核心层投影；TUI 只选择颜色、布局和展开方式，不识别 `Read`、`Bash` 等具体工具，也不从原始 tool result 猜测摘要。发送给模型的内容和用户展示内容是独立投影，UI 截断不改变模型上下文。

TodoPanel 的初始值来自 `RuntimeStatus.todos`，resume 使用目标 session 的同一快照；回合
内则消费 `TodoListUpdated`。非空列表首次出现时展开，Ctrl+T 在完整列表与计数/当前项
摘要之间切换，空列表自动隐藏。混合列表保留 completed 项并弱化显示；ActivityBar
优先使用 in-progress 项的 `active_form`。面板有界滚动，长列表不会占满会话区域。

## 权限输入

权限询问使用 composer 内的 `PermissionPanel` 临时替换普通输入框，而不是居中遮罩。基础选项为 `Yes`、`No` 和 `No, and tell nano-code why`。工具提供安全的长期授权建议时才增加 `Yes, and don't ask again`：Bash 要求输入命令前缀（如 `git diff:*`），Write/Edit 使用当前规范化路径的精确规则，均写入 local settings。safety ask 不提供长期允许。拒绝反馈返回模型但审计日志不记录原文；Esc 和无 UI 环境始终按普通拒绝处理。

## Slash 命令

命令注册表保存名称、别名、说明和本地 action。以 `/` 开头的输入先由注册表处理，未知命令不会发送给模型。OptionList 只消费注册表元数据，支持前缀过滤、方向键选择、Tab 补全和 Enter 执行。

当前 `/clear` 只清理已渲染消息，不删除 Transcript 或运行时上下文。长消息列表虚拟化尚未实现；模型增量、工具状态和权限请求均由 runtime 事件驱动，TUI 不轮询核心对象。

`/provider` 打开独立的 ProviderScreen。协议、URL、模型、reasoning 开关及协议相关 effort/context 可见；API Key 使用 password Input，空值表示保留旧 Key，ProviderView 只暴露 `has_stored_key` 布尔值。保存后由 runtime 持久化并切换 ProviderRouter，TUI 不直接读写配置文件。

`/resume` 打开独立的 ResumeScreen。列表只展示当前项目的其他可恢复会话：首行是第一次用户输入的归一化预览，次行右侧是相对修改时间；支持方向键、Enter 和 Esc。选择后由 runtime 严格加载目标会话并原子切换 session-scoped 状态，再把历史投影为 `HistoryEntry` 返回 TUI 重建消息组件。工具结果优先复用 Transcript 中的展示快照；旧记录才由 Tool 重新投影。选择器和 TUI 均不读取 JSONL，也不持有核心消息类型。

`/context` 通过 runtime DTO 展示输入 token 估算、输出预留、system/tools/messages
字符组成、工作集消息数和压缩次数，不会触发上下文变更。`/compact` 在普通 activity
状态下执行手动摘要并显示新预算。TUI 不导入 ContextPlanner、CompactBoundary 或
SessionStore；自动 compact 和 reactive compact 同样由 Agent 层决定。

CLI 组合根把 `ProviderRouter`、`ToolRoundExecutor`、`ConversationState`、
`ContextPlanner` 和 `CompactionCoordinator` 装配进 `AgentEngine`。`DefaultChatRuntime`
只通过 `AgentInboundPort` 的 status/context status、事件和 session 操作生成 DTO；session
切换时由 runtime 在同一锁内切换 Transcript 与工具结果目录。
