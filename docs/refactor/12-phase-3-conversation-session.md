# 阶段 3：收拢 Conversation 与 Session

完成日期：2026-08-19。

## 最终边界

- `conversation` 拥有消息、ToolCall、ToolResult、compact 事实以及纯内存 `Conversation` 聚合；没有文件 I/O，也不依赖 Agent、Context、Session、Chat 或 UI。
- `sessions` 拥有具体 `Session`、JSONL record/codec/store/catalog、恢复校验和未闭合工具轮修复；没有单实现 Repository Protocol。
- `context.ContextSession` 拥有不落盘的 live-session attachment delivery，并在 resume 时整体替换。
- `tools` 拥有通用 Tool presentation。`ToolResult` 不再携带展示字段；Session 通过独立 `tool_presentation` add-on record 和内存索引恢复历史展示。

## 提交与恢复不变量

`Session` 先在 Conversation 副本上验证变更，再写 JSONL，成功后才替换活动聚合。消息与 compact 写入失败都不会推进内存；compact 的 replacement、boundary、summary 和 metadata 通过一次 store append 提交。

恢复先构造候选 `Session`，严格解析并将尾部未闭合 ToolCall 的错误结果写盘。空日志、损坏日志或修复写入失败均不会产生可供 Chat 切换的候选对象。Chat 的完整 Session bundle 原子切换仍按计划在阶段 6 收口。

Transcript 继续使用 schema v5。新增 `tool_presentation` 是 v5 的可选 add-on record；codec 仍能读取旧 v5 中内嵌于 tool result 的 presentation，但只恢复到 Session 展示索引，不写回 Conversation fact。

## 前序阶段复核

阶段 1 的 Pyright standard 配置仍覆盖 `src` 与 `tests`。阶段 2 的 `model` 仍无跨模块出边，且仓库仍只有一个 `ModelClient` Protocol。阶段 3 同时修正了阶段 0 债务表中的过时例外：Conversation 已退出主循环分量，所有 phase-3 依赖和深层 import 例外均已删除。

与阶段 2 完成时相比，跨模块 import 从 236 降至 225，临时违规模块边从 37 降至 30，临时深层 import 边从 44 降至 36。主循环分量从 12 个模块收缩为 9 个；Chat/FileMention 形成的二节点循环保留给阶段 6。

## 验收

- Ruff format/check 通过；
- Pyright standard 通过，0 errors、0 warnings；
- pytest 全量通过；
- AST 架构守卫通过，phase-3 例外为零。
