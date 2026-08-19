# 阶段 5：Context 与 Features

## 最终边界

- `context` 拥有 Conversation 到 `ModelRequest` 的投影、预算、attachment delivery、microcompact 和 full compact 候选。
- `ContextBuilder` 不保存 session 事实；每次 build 的 request、budget 和 proposal 用完即弃。
- `ContextSession` 只保存当前活动 session 的 live delivery、user context cache 和 session-stable prompt cache；resume 时整体替换。
- `features.todos` 拥有 Todo 模型、编解码、Conversation 投影、reminder 和 `TodoWriteTool`。
- `features.file_mentions` 拥有 mention 解析、读取、attachment 加载、建议 DTO 和短期路径索引。
- Agent、Context、Tools 等基础模块不导入具体 feature；组合根注册 Todo 工具和 reminder，Chat 执行 Todo UI 投影。

Context 的 `ContextBudget`、`ContextPlan`、`CompactionOutcome` 和 `ContextOverflow` 已从 Agent 移回 Context。单实现的 `ContextPort` 与 `CompactorPort` 已删除，避免 Context 反向实现 Agent 声明的接口。

## 生命周期

```text
runtime: PromptRegistry static cache
session: ContextSession delivery + user context + session prompt cache
request: ModelRequest + ContextBudget + attachment resolution + compact proposal
```

新 `ContextSession` 不继承旧对象的 delivery 或 cache。Conversation history 仍只由 Conversation/Session 维护；ContextSnapshot 是一次投影输入，不是第二份可变消息存储。

## 前序阶段复查

- 阶段 2 的 provider continuation 仍只在 binding 匹配时回放。
- 阶段 3 的 Conversation、持久化优先提交和 compact working set 不变量保持不变。
- 阶段 4 的 Tool → Permission/Workspace 单向依赖保持不变；Todo Tool 由 bootstrap 作为 feature 能力显式加入 Registry。
- 未创建 Subagent 或 Plan Mode 占位目录和抽象。

阶段 5 清除了全部本阶段依赖与深层 import 例外。生产 Python 文件为 128 个，跨模块 import 为 190 条，临时违规模块边从 27 降至 23，临时深层 import 边从 30 降至 19。依赖环只剩阶段 7 的 `core/permissions/providers` 和 `bootstrap/cli`。

## 验证

- Todo 与 file mention 行为测试继续通过。
- 新增 ContextSession cache 隔离、resume 清空 live delivery 和基础模块不导入 feature 的回归测试。
- Ruff、Pyright standard、pytest 与 AST 架构守卫全部通过。
