# 上下文管理

## 职责

`context` 把 Conversation facts 转换为一次 provider-neutral `ModelRequest`。它维护请求预算、规范化、工具结果替换、attachment delivery、用户上下文和 compact 计划，但不拥有持久化消息。

主要模块：

- `context.planner`：构造请求与提交前决策。
- `context.session`：live-session delivery 和缓存。
- `context.models`：预算、计划与压缩结果。
- `context.compaction`：摘要模型调用与 compact proposal。
- `context.attachments`：不写 Transcript 的临时上下文。

## 请求组成

```text
SystemPrompt
+ User context
+ Conversation working set
+ request/live-session attachments
+ Tool definitions
=> ModelRequest
```

Context 会把 Conversation 内容转换为 Model message/block，但不会修改 canonical facts。只有 `ContextPlan` 中明确列出的 content replacement 或 attachment delivery 在请求使用前提交到各自所有者。

## 预算

`ContextBudget` 同时记录字符规模、tokenizer 估算、provider 上报校准、输出预留和模型限制来源。触发阈值来自当前 `ActiveModelEnvironment`；provider 切换只在模型调用之间原子更新该环境。

超限顺序：

1. 对旧的大型工具结果建立 content replacement。
2. 若仍超过阈值，执行完整 compact。
3. provider 返回 context overflow 时允许一次 reactive compact。
4. 仍无法构造请求则抛出 `ContextOverflow` 或 `ModelContextOverflow`。

## Compact

`CompactionCoordinator` 只返回 `CompactionOutcome`，不直接写 Session。Agent 收到 outcome 后通过 `Session.commit_compaction()` 一次提交 replacements、summary 和 boundary。

摘要不是隐藏缓存，而是新的 `ConversationSummaryMessage`。恢复时 Session 根据 compact boundary 重建工作集，因此 compact 后仍可继续对话。

## ContextSession 生命周期

每个活动 Session 对应一个 `ContextSession`，其中包含：

- 已交付 attachment 及其 anchor。
- 首次解析后复用的用户上下文。
- 当前 session 的稳定 prompt section cache。

切换或恢复 Session 时必须创建新实例；这些状态不持久化，也不能跨 Session 复用。
