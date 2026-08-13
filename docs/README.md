# Claude Code 核心机制阅读笔记

本目录记录 `claude-code/` 参考源码中的核心运行机制，为 nano-code 的 Python 实现提供架构依据。内容以源码为准，不是产品使用手册，也不追求覆盖所有功能。

参考快照包含大量实验开关和内部功能。同一机制可能存在多条实现路径，文档优先描述主路径，并在必要时标出 feature-gated 分支。

## 架构地图

```text
QueryEngine（跨用户输入保存会话状态）
    │
    └── query（一次 agentic turn 的状态机）
          ├── 上下文投影 / token 预算 / compact
          ├── 模型流式调用
          ├── Tool Registry + Tool Executor
          │       └── Hooks → Permission → Tool.call → Result
          └── 消息事件流 → UI / SDK / JSONL Transcript

MCP Client ──> 动态 Tool ──> Tool Registry
ToolSearch ──> 按需暴露 deferred tool schema
```

## 阅读顺序

| 文档 | 只回答的问题 |
| --- | --- |
| [01-agent-loop.md](01-agent-loop.md) | 一次用户输入如何驱动多轮模型与工具调用？ |
| [02-messages-and-sessions.md](02-messages-and-sessions.md) | 消息如何表示、持久化、分支和恢复？ |
| [03-context-management.md](03-context-management.md) | 发给模型的上下文如何计量、裁剪和压缩？ |
| [04-tool-system.md](04-tool-system.md) | 工具如何定义、注册、调度和返回结果？ |
| [05-permission-system.md](05-permission-system.md) | 权限规则如何组合，最终决策顺序是什么？ |
| [06-mcp-and-tool-discovery.md](06-mcp-and-tool-discovery.md) | MCP 工具如何接入，以及为何需要延迟加载？ |
| [07-hooks.md](07-hooks.md) | Hooks 在哪些阶段介入，能改变什么？ |
| [08-mvp-scope.md](08-mvp-scope.md) | Python MVP 当前实现什么，哪些能力留到后续？ |
| [09-storage-and-settings.md](09-storage-and-settings.md) | 会话、工具结果和分层配置分别存在哪里？ |
| [10-terminal-ui.md](10-terminal-ui.md) | TUI、slash 命令和核心 runtime 如何解耦？ |

## 统一术语

- **用户回合（user turn）**：用户提交一次输入，由 `QueryEngine.submitMessage()` 启动。
- **循环迭代（iteration）**：`query()` 中的一次“准备上下文 → 调模型 → 处理结果”。一次用户回合可能包含多次迭代。
- **工具轮（tool round）**：模型返回 `tool_use`，运行工具并把 `tool_result` 放入下一次迭代。
- **内部消息**：运行时的 `Message` 联合类型，包含 UI 和控制事件。
- **API 消息**：`normalizeMessagesForAPI()` 投影出的 user/assistant 消息。
- **Transcript**：写入 JSONL 的可恢复会话记录。
- **microcompact**：主要清理旧工具结果，不生成整段对话摘要。
- **compact**：生成对话摘要并建立新的上下文边界。

## 阅读边界

当前文档不展开远程会话、团队协作、浏览器控制、统计上报和具体模型提示词。它们会使用这里的内核，但不是 nano-code 第一阶段需要复刻的架构。

源码行号来自当前仓库快照，后续更新参考源码时可能漂移；文件与符号名比固定行号更可靠。
