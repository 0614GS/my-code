# 当前 MVP 范围

## 已实现

- Anthropic Messages 与 OpenAI Responses provider。
- provider-neutral 流式 text、reasoning、usage 和 continuation 建模。
- 多 step Agent loop、带安全屏障和并发上限的 ToolRound，以及 step 上限。
- 默认关闭的 Subagent：固定 `explore`/`general` 角色、独立 child Session/run/provider lease/prompt、foreground/background、Task 查询/取消/单次通知、权限收窄与多维预算。
- 默认关闭的 MCP stdio server：分层配置、保守 project trust、连接/重连/关闭、增量发现、deferred ToolSearch 与标准权限执行。
- 默认关闭的 Skill：project/user/builtin 分层发现、严格 frontmatter、lazy load、原子 reload、Conversation attachment 激活与 additive session 权限规则。
- Read、Write、Edit、Glob、Grep、Bash 与 TodoWrite。
- allow/ask/deny 规则、交互确认、dontAsk 和 bypassPermissions。
- 工作区边界、受保护路径和 Bash 静态权限分析。
- Conversation canonical facts、JSONL Session、catalog 与原子恢复。
- token budget、工具结果替换、自动/手动/reactive compact。
- `@path` 文件或目录 attachment、路径补全与 AGENTS 用户上下文。
- provider profile、凭据存储、模型发现缓存和运行期切换。
- 非交互 print 模式与 Textual TUI。
- Ruff、Pyright standard、pytest 和 AST 架构守卫。

## 明确延后

- MCP 2026-07-28 driver、resources/prompts 和富媒体结果。
- Hooks。
- Plan Mode 产品能力。
- Session fork、远程会话和跨设备同步。
- 媒体 attachment。
- OAuth、系统 Keychain 和团队凭据管理。
- OS 级容器或系统调用 sandbox。
- 断线续传和 provider stream 重放。

## 保留的不变量

- Provider SDK 类型不离开 `providers`。
- Session 是 canonical conversation 的唯一权威来源。
- Session records 不成为领域模型。
- Context 是请求时投影，不维护第二份可写历史。
- ToolCall 在执行前已经作为完整 AssistantMessage 提交。
- 权限拒绝和取消路径仍产生闭合结果。
- 活动 Session 与其 `ContextRuntime` 原子配对；prompt/user-context cache 不会跨恢复或 child run 混用。ToolResult presentation 内嵌并可恢复，provider replay 仍以 sidecar 关联；durable Attachment 可恢复。

## 与参考实现的关系

`claude-code/` 用于学习行为和安全不变量。my-code 不机械复刻其实验功能、压缩产物、ports/adapters 或内部包结构；有意差异以本文件和模块文档为准。

参考实现的 Agent Tool 主要用 `maxTurns` 限制 child，且全局 token-budget continuation 不用于 subagent。my-code 有意保留等价的 `max_steps`，并额外在 child run 上设置累计 token ceiling；已完成的 provider 响应会保留，预算在下一次请求前阻止继续消费。

本项目采用参考实现风格的 fresh child 与只读 Explore，但有意保留 Codex 风格的显式 role definition、独立 run/Session 和能力收窄。角色不可由用户扩展；Explore 是不能派生 child 的只读叶子，General 继承 spawn step 的普通/动态/Skill/MCP/Task 工具，并在深度允许时继续派生两种角色。

并行工具、foreground/background Subagent、MCP 与 Skill 已分别按路线图 M2、M4a/M4b、M5、M6 实现。Skill 首版有意使用严格平面 frontmatter、只把 Markdown 作为数据加载，并不复刻参考实现中动态命令、脚本执行或完整 YAML 语义；MCP resources/prompts transport 也仍延后。参考实现将 Skill 正文表示为 meta UserMessage；本项目有意统一表示为 `AttachmentMessage`，但投影后的模型输入语义相同。边界与验收证据见 [13-extensibility-roadmap.md](13-extensibility-roadmap.md)。
