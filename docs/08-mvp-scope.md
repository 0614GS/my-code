# 当前 MVP 范围

## 已实现

- Anthropic Messages 与 OpenAI Responses provider。
- provider-neutral 流式 text、reasoning、usage 和 continuation 建模。
- 多 step Agent loop、串行 ToolRound 和 step 上限。
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

- MCP、远端工具发现和 deferred tools。
- Hooks。
- Subagents 和 Plan Mode 产品能力。
- 并行工具执行。
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
- 活动 Session 的 context cache/delivery、结果绑定与 replay sidecar 不会跨恢复混用。

## 与参考实现的关系

`claude-code/` 用于学习行为和安全不变量。my-code 不机械复刻其实验功能、压缩产物、ports/adapters 或内部包结构；有意差异以本文件和模块文档为准。
