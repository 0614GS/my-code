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
└── composer
    ├── OptionList (slash commands)
    ├── ActivityBar
    ├── PermissionPanel (inline)
    ├── Input
    └── StatusBar

ProviderScreen (modal)
        ↕
ChatRuntime protocol
        ↕
CliChatRuntime ── AgentEngine
```

TUI 只通过 `ChatRuntime` 的事件流提交输入、读取安全状态、配置无凭据展示 DTO 并注册权限处理器，不接触 Provider SDK、ToolExecutor、SessionStore 或 AgentEngine。CLI composition root 使用 `DeferredPermissionPrompter` 把核心权限询问转换成 UI DTO；无处理器时仍然 fail closed。

## 流式显示与持久化边界

Anthropic 适配器把 SSE 文本 delta 转换为 provider-neutral 事件，Agent 再投影成 TUI 事件。增量文本只进入临时 `AssistantMessage`，并以约 30 FPS 的短批次合并后交给 Rich Markdown 渲染；只有收到 SDK 的完整最终 Message 后，Agent 才验证并持久化 assistant message、读取完整工具参数和执行工具。连接中断时屏幕可保留部分文本用于反馈，但 Transcript 不写入半截响应。

工具开始时创建稳定的 `ToolCallMessage`，结束时用相同 tool ID 原位更新。界面只显示识别参数、结果数量或首行预览；发送给模型和保存到 Transcript 的 tool result 不受 UI 截断影响。

## 权限输入

权限询问使用 composer 内的 `PermissionPanel` 临时替换普通输入框，而不是居中遮罩。选项为 `Yes`、`No`、`No, and tell nano-code why`。第三项打开单行反馈输入；反馈随拒绝的 tool result 返回模型，使下一轮能够调整做法。Esc 和无 UI 环境始终按普通拒绝处理。

## Slash 命令

命令注册表保存名称、别名、说明和本地 action。以 `/` 开头的输入先由注册表处理，未知命令不会发送给模型。OptionList 只消费注册表元数据，支持前缀过滤、方向键选择、Tab 补全和 Enter 执行。

当前 `/clear` 只清理已渲染消息，不删除 Transcript 或运行时上下文。长消息列表虚拟化尚未实现；模型增量、工具状态和权限请求均由 runtime 事件驱动，TUI 不轮询核心对象。

`/provider` 打开独立的 ProviderScreen。URL、模型和 Profile ID 可见；API Key 使用 password Input，空值表示保留旧 Key，ProviderView 只暴露 `has_stored_key` 布尔值。保存后由 runtime 持久化并切换 ProviderRouter，TUI 不直接读写配置文件。环境变量覆盖仍优先于已保存值。
