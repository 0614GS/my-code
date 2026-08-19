# 状态管理重构验收标准

本清单定义最终行为，不以类名或文件移动代替验收。每项必须有直接自动化测试或架构守卫证据。

状态列由实现阶段维护：`待实现`、`进行中`、`已验收`。

## 术语与 Agent loop

| ID | 状态 | 验收行为 |
| --- | --- | --- |
| T1 | 待实现 | 只有真实 `HumanMessage` 开始新 turn，attachment、reminder 和 tool output 不开始 turn。 |
| T2 | 待实现 | 一次模型调用计为一个 step；一个 turn 可包含多个 step。 |
| T3 | 待实现 | 每个成功完成的 step 恰好提交一个完整 `AssistantMessage`。 |
| T4 | 待实现 | 失败或取消的 Provider 流不会提交部分 AssistantMessage，但运行期 step 状态会被清理。 |
| T5 | 待实现 | `max_steps` 限制的是一个 turn 内的模型调用次数，不限制 turn 数或 ToolCall 数。 |
| T6 | 待实现 | 一个包含工具调用的 step 完成工具闭合后，可以在同一 turn 继续下一个 step。 |

## 状态所有权

| ID | 状态 | 验收行为 |
| --- | --- | --- |
| O1 | 待实现 | AppState 是 workspace、active Session、runtime permissions 和 ProviderRuntime 的唯一活动入口。 |
| O2 | 待实现 | Session 是 canonical conversation、working set、session context state、工具结果绑定和 replay sidecar 的唯一所有者。 |
| O3 | 待实现 | ChatService 不持有与 AppState 重复的 session/provider/permission 可变状态。 |
| O4 | 待实现 | AgentEngine、ContextEngine、ToolExecutor 和 Provider adapter 均不持有 AppState。 |
| O5 | 待实现 | Context、Agent、Chat 和 TUI 不保存第二份可写 conversation entries。 |
| O6 | 待实现 | request、turn、step、tool round 和 streaming 状态不会提升到 AppState 或 Session。 |
| O7 | 待实现 | 派生 Todo 状态通过完整 Session history 投影，不维护第二份可变 Todo list。 |

## Session 与持久化

| ID | 状态 | 验收行为 |
| --- | --- | --- |
| S1 | 待实现 | Session 调用方只使用 snapshot 和语义提交方法，不接触 JSONL record、codec、store 或路径。 |
| S2 | 待实现 | 进程内读取只使用已打开 Session 的内存状态，不把磁盘当 refresh API。 |
| S3 | 待实现 | 所有提交先验证候选、再持久化、最后替换内存状态。 |
| S4 | 待实现 | message、tool result、presentation、externalized result、replay 和 compaction 任一写入失败时，内存状态不部分推进。 |
| S5 | 待实现 | 恢复严格校验 schema、父链、重复 ID、tool pairing、compact boundary 和 replay 关联。 |
| S6 | 待实现 | 尾部未闭合 ToolCall 在恢复时得到稳定错误结果，并且修复可再次恢复。 |
| S7 | 待实现 | 旧 transcript 可读取；格式升级不会静默丢失 message、tool result、usage、presentation 或 continuation。 |
| S8 | 待实现 | 目标 Session 恢复失败时，AppState 仍完整引用旧 Session。 |
| S9 | 待实现 | 切换 Session 会同时切换 conversation、context delivery/cache、工具结果和 replay sidecar。 |

## Conversation 与工具协议

| ID | 状态 | 验收行为 |
| --- | --- | --- |
| C1 | 待实现 | `ToolResultBatch` 是独立 ConversationEntry，不属于 HumanMessage 或 AssistantMessage。 |
| C2 | 待实现 | 工具执行前，包含 ToolCall 的完整 AssistantMessage 已经提交。 |
| C3 | 待实现 | 一个 ToolResultBatch 恰好闭合 source AssistantMessage 中的全部 ToolCall。 |
| C4 | 待实现 | 重复 call ID、重复 result、孤立 result、缺失 result 和错误 source assistant 都被拒绝。 |
| C5 | 待实现 | 工具取消和异常为所有未完成调用生成错误结果，不留下无法继续请求的轨迹。 |
| C6 | 待实现 | full/micro compact 不产生孤立 ToolCall 或 ToolOutput，也不跨非法边界裁剪。 |
| C7 | 待实现 | 多工具 batch 在 Session 中保持确定顺序，并在两个 Provider 中保持正确配对。 |

## Context 与投影

| ID | 状态 | 验收行为 |
| --- | --- | --- |
| X1 | 待实现 | ContextEngine 对相同 SessionSnapshot、RequestContext 和 ModelEnvironment 产生确定性 ContextPlan。 |
| X2 | 待实现 | ContextEngine 不修改 Session，也不缓存 conversation history。 |
| X3 | 已实现 | attachment、Todo reminder 和 user context 以 provider-neutral input item 注入。 |
| X4 | 待实现 | Todo reminder 不写 canonical transcript；失败 TodoWrite 不覆盖最后成功的 Todo 状态。 |
| X5 | 待实现 | live-session attachment delivery 在同一 Session 可重放，resume/switch 后按声明清空或重建。 |
| X6 | 已实现 | Context 注入不会插入到 ToolCall 与对应 ToolOutput 的非法协议位置。 |
| X7 | 已实现 | budget、trim 和 compaction 基于公共 ModelInputItem，不依赖 OpenAI/Anthropic SDK 类型。 |
| X8 | 待实现 | full compact proposal 失败不改变 Session；成功必须通过 Session 原子提交。 |

## Provider-neutral ModelRequest

| ID | 状态 | 验收行为 |
| --- | --- | --- |
| M1 | 已实现 | `ModelRequest.input` 是有序 `ModelInputItem`，不再以 `tuple[ModelMessage]` 为核心抽象。 |
| M2 | 已实现 | `ToolOutputs` 与 `UserInput`、`AssistantOutput` 并列。 |
| M3 | 已实现 | 公共模型不包含 OpenAI/Anthropic SDK 类型或 wire 字段名要求。 |
| M4 | 已实现 | common validation 不依赖 user/assistant role 识别 ToolCall/ToolOutput。 |
| M5 | 已实现 | text、reasoning 和 tool call 的相对顺序从 Provider output 到 Session 再到后续 input 保持稳定。 |
| M6 | 待实现 | Provider replay payload 与 canonical content 分离，并通过 entry/content ID 关联。 |
| M7 | 已实现 | binding 不匹配时仍保留 canonical content，但不发送 replay payload。 |
| M8 | 已实现 | compaction 后不发送已离开工作集的 replay payload。 |

## OpenAI Responses 映射

| ID | 状态 | 验收行为 |
| --- | --- | --- |
| OR1 | 已实现 | Human/UserInput 映射为 OpenAI message input item。 |
| OR2 | 已实现 | Assistant text、reasoning 和 ToolCall 分别映射为合法 Responses items。 |
| OR3 | 已实现 | 每个 ToolOutput 映射为顶层 `function_call_output`，不会先构造 user-role tool-result message。 |
| OR4 | 待实现 | function call 使用 `call_id` 配对；response item `id` 只存在于 replay sidecar。 |
| OR5 | 已实现 | 多个并行 function call/output 保持顺序和一一对应。 |
| OR6 | 已实现 | error ToolOutput 有稳定、可测试且模型可理解的编码。 |
| OR7 | 已实现 | `store=False`、reasoning encrypted content 和匹配 replay 行为保持现有能力。 |
| OR8 | 待实现 | OpenAI 流事件只形成短生命周期 delta；最终 response snapshot 才能进入 Session。 |

## Anthropic Messages 映射

| ID | 状态 | 验收行为 |
| --- | --- | --- |
| AM1 | 已实现 | UserInput 映射为 user-role message content。 |
| AM2 | 已实现 | AssistantOutput 映射为 assistant-role text/thinking/tool_use blocks。 |
| AM3 | 已实现 | ToolOutputs 仅在 Anthropic adapter 中包装成 user-role tool_result blocks。 |
| AM4 | 已实现 | 相邻 role 合并和 user/assistant 归一化只存在于 Anthropic adapter。 |
| AM5 | 待实现 | tool_result 的 `is_error`、多个结果和媒体内容得到正确映射。 |
| AM6 | 已实现 | thinking signature/redacted thinking 仅在 binding 和 scope 匹配时重放。 |
| AM7 | 待实现 | Anthropic 流事件只形成短生命周期 delta；final Message 才能进入 Session。 |

## Runtime 切换、并发与失败

| ID | 状态 | 验收行为 |
| --- | --- | --- |
| R1 | 待实现 | submit、stream、compact、resume 和 provider switch 不能并发修改同一 AppState。 |
| R2 | 待实现 | 流式 step 进行中不能部分切换 Session 或 Provider。 |
| R3 | 待实现 | Provider switch 原子替换 connection、client、capabilities 和 model environment，并关闭旧 client。 |
| R4 | 待实现 | Provider switch 不改写 Session canonical facts。 |
| R5 | 待实现 | Permission update 持久化失败时 runtime policy 不改变。 |
| R6 | 待实现 | pending approval 完成、拒绝、异常或取消后都从 runtime 中释放。 |
| R7 | 待实现 | runtime close 释放 Provider client 和 host task，不破坏已提交 Session。 |
| R8 | 待实现 | Session/AppState 切换失败不产生旧 conversation 配新 context/tool store/provider 的混合状态。 |

## 架构与质量门槛

| ID | 状态 | 验收行为 |
| --- | --- | --- |
| A1 | 待实现 | AST 依赖图无环，且 AppState 只被允许的 application/bootstrap/host 模块依赖。 |
| A2 | 待实现 | Provider SDK 类型只出现在 `providers`。 |
| A3 | 待实现 | Session record、codec 和存储路径不泄漏出 `sessions` 私有实现。 |
| A4 | 待实现 | 不存在两个可写的 conversation 或 runtime permission 权威来源。 |
| A5 | 待实现 | `uv run ruff format .` 无待格式化文件。 |
| A6 | 待实现 | `uv run ruff check .` 通过。 |
| A7 | 待实现 | `uv run pyright` 通过。 |
| A8 | 待实现 | `uv run pytest` 通过。 |

## 完成定义

只有同时满足以下条件，状态管理重构才可以标记完成：

- 上述验收项全部为“已验收”；
- 每项行为能够指向自动化测试或架构守卫；
- 旧 transcript 可以恢复，失败路径没有双重状态；
- 顶层当前实现文档已经同步；
- 旧 API 和迁移兼容层已删除；
- 任意 runtime 状态都能回答“谁拥有、活多久、是否持久化、何时失效”。
