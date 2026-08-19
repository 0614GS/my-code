# 状态生命周期测试清单

本清单把 [03-state-lifecycle.md](03-state-lifecycle.md) 的状态表转成可验证行为。`已有` 表示阶段 0 时已有直接测试；`待阶段 N` 表示目标边界建立时必须补齐或迁移测试。后续阶段不得以模块移动为由删除这些行为。

## 恢复

| ID | 必须验证的行为 | 当前证据或落地阶段 |
| --- | --- | --- |
| R1 | JSONL 严格校验 schema、首记录、UUID、父链和重复项 | 已有 `test_session_store.py`；阶段 3 迁入 Session 测试 |
| R2 | 恢复重建完整 history、活动父链、replacement、compact boundary 和同一 working set | 阶段 3 已由 Conversation/Session 组合测试覆盖 |
| R3 | 尾部未闭合 ToolCall 恢复为错误 ToolResult，且修复先落盘 | 已有 `test_resume_repairs_trailing_tool_calls` |
| R4 | 目标为空、损坏或修复写入失败时，当前 Session bundle 不变 | 阶段 3 已覆盖空目标、严格损坏和修复写入失败；整个 bundle 切换留阶段 6 |
| R5 | Todo 从完整 history 投影，compact 前的最后有效状态仍可恢复 | 已有 Todo history/compact 投影测试 |
| R6 | 工具展示使用持久快照；旧记录缺少快照时可退回当前 Tool 投影 | 阶段 3 已覆盖独立 add-on record/index 与 fallback |
| R7 | provider binding 匹配才恢复 continuation；不匹配时保留消息但丢弃 continuation | 阶段 2/3 已由 provider mapper、normalizer 与预算回归测试覆盖 |
| R8 | resume 使用当前 provider、permission 和 workspace，不从 Transcript 覆盖运行时配置 | 待阶段 6 |

## Session 切换

| ID | 必须验证的行为 | 当前证据或落地阶段 |
| --- | --- | --- |
| S1 | 先完整构造候选 Session，再一次替换活动引用 | 当前 ConversationState 有局部测试；阶段 6 在 ChatService 验证整个 bundle |
| S2 | Session、Conversation、ContextSession、工具结果目录和展示索引一起切换 | 待阶段 6 |
| S3 | submit、stream、compact 与 resume 互斥，流式 turn 中不能切换 | 当前 Chat runtime 共用锁；阶段 6 增加并发回归测试 |
| S4 | 切换失败后 session ID、history、context delivery 和工具结果 binding 均保持原值 | 待阶段 6 |
| S5 | TUI/CLI 只接收 Chat 投影，不持有核心消息，也不读取 JSONL | 部分架构测试已有；阶段 6 收口 |
| S6 | 切换 provider 关闭旧连接但不改写历史；活动模型能力随新连接更新 | provider 关闭测试已有；阶段 2/6 补历史与能力断言 |

## 提交与失效

| ID | 必须验证的行为 | 当前证据或落地阶段 |
| --- | --- | --- |
| I1 | 消息、replacement 和 boundary 都先写盘后改内存；写盘失败不推进内存 | 阶段 3 已覆盖消息与整组 compact 提交失败 |
| I2 | 用户消息在首次模型调用前落盘，完整 assistant 在工具执行前落盘 | 已有 Agent engine 测试；阶段 6 保留 |
| I3 | request attachment、预算、microcompact proposal 和 provider 流状态在调用后丢弃 | 阶段 5 的无状态 ContextBuilder 与逐次 attachment 测试覆盖；provider 流状态在阶段 2 验证 |
| I4 | live-session delivery 跨当前 session 后续 turn 重放，锚点离开 working set 后裁剪 | 跨 turn 与 compact 裁剪已有 |
| I5 | resume、switch 或销毁 ContextSession 时清空 live-session delivery 和 session cache | 阶段 5 已覆盖 resume delivery 与 ContextSession cache；完整 bundle switch 留阶段 6 |
| I6 | user context/prompt cache 按 runtime、session、request 生命周期分别失效 | 阶段 5 已覆盖 static、session 和 request 三种失效边界 |
| I7 | full compact 摘要失败不改变 Session；成功后重新计算 working set 与派生状态 | 阶段 3–5 已覆盖失败无提交、成功重建与 reminder 派生 |
| I8 | 工具取消、异常和进程恢复都闭合已提交 ToolCall，不留下非法模型协议 | 阶段 4 已覆盖取消补齐、工具异常与稳定错误结果；恢复组合留阶段 6 |
| I9 | pending approval 与 turn 状态在完成或取消后销毁；runtime permission policy 继续存活 | 阶段 4 已覆盖 permission prompt 取消且工具不执行；turn 生命周期留阶段 6 |
| I10 | Todo reminder delivery 不写 Transcript，失败 TodoWrite 不覆盖最后成功状态 | 已有直接测试 |

## 执行规则

- 每个后续阶段先引用对应 ID，再增加或迁移测试。
- `已有` 只代表当前行为基线；模块所有者变更后测试应迁到新领域目录。
- 失败、取消和部分写入路径与成功路径同等属于验收范围。
