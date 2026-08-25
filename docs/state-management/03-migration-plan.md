# 状态管理迁移计划

本迁移按可回滚的小阶段执行。每个阶段必须保持 `uv run mycode --help`、类型检查和测试可运行，不采用一次性重写。

## 总体顺序

```text
0. 固定术语与行为基线
        ↓
1. 建立 item-based ModelRequest
        ↓
2. 两个 Provider adapter 改用新输入项
        ↓
3. Session 收拢 Conversation 与持久化
        ↓
4. Session 收拢 context/replay/tool-result 状态
        ↓
5. 引入 AppState 并简化 ChatService
        ↓
6. 删除兼容模型并加强架构守卫
        ↓
7. 文档与最终验收
```

## 0. 固定基线与待决项

- [x] 为 turn、step、tool round 增加直接术语测试或测试注释。
- [x] 增加一个 turn 内多 step 的 characterization test。
- [x] 固定 OpenAI Responses 当前 function_call/function_call_output 序列快照。
- [x] 固定 Anthropic Messages 当前 assistant tool_use/user tool_result 序列快照。
- [x] 固定 provider continuation 匹配、不匹配和 compact 后裁剪行为。
- [x] 固定 Session 写入失败、resume 失败和 tool cancellation 行为。
- [x] 决定目标类名：`ToolResultBatch` 或 `ToolResultsEntry`。
- [x] 决定 `AppState` 的最终模块路径，避免与通用 `state.py` 混淆。
- [x] 决定显式用户附件持久化引用、内容快照及缺失文件的恢复语义。
- [x] 决定 replay sidecar 的 JSONL record 版本和旧记录兼容策略。

阶段 0 的证据索引与冻结决策见 [00-baseline-decisions.md](00-baseline-decisions.md)。

退出条件：现有关键行为都有测试证据，所有待决项记录为明确决策，后续阶段不需要猜测数据语义。

## 1. 建立 item-based ModelRequest

- [x] 在 `model` 中增加 `ModelInputItem` 联合。
- [x] 增加 `UserInput`、`AssistantOutput` 和 `ToolOutputs`。
- [x] 将 text、image、document、reasoning、tool call 和 tool output 设计为封闭的判别联合。
- [x] 将 tool pair 校验改为遍历有序 input items，不依赖 role。
- [x] 保持 `ModelOutput` 与 request input 类型方向清晰，避免输入和输出 DTO 相互滥用。
- [x] 增加构造校验：空内容、重复 call ID、孤立 output、重复 output 和未闭合 call。

退出条件：新 IR 类型及纯转换测试可以无损表达当前所有 context 输出，且没有 `ToolOutput is user content` 关系；本阶段不增加第二个生产请求入口。

## 2. 迁移 Provider adapters

### OpenAI Responses

- [x] 将 `ModelRequest.messages` 一次性替换为 `ModelRequest.input`，不保留双字段生产 API。
- [x] 让 adapter 直接消费 `ModelRequest.input`。
- [x] `UserInput` 映射为 message input item。
- [x] `AssistantOutput` 按内容顺序映射为 message/reasoning/function_call items。
- [x] `ToolOutputs` 映射为顶层 function_call_output items。
- [x] 保持 call ID 与 OpenAI response item ID 分离。
- [x] 保持 `store=False` 和当前 continuation replay 行为。
- [x] 为 error tool output 定义稳定 wire 编码并测试。

### Anthropic Messages

- [x] 让 adapter 直接消费 `ModelRequest.input`。
- [x] 在 adapter 内完成 user/assistant role grouping。
- [x] 将 `ToolOutputs` 包装为 user-role tool_result blocks。
- [x] 保持 thinking signature/redacted thinking replay。
- [x] 验证相邻 role、多个 tool result、image/document content 的映射。

### 清理 Context 泄漏

- [x] 从 Context normalizer 删除 `_merge_adjacent` role 合并。
- [x] Context 不再创建 `ModelUserMessage(tool_result)`。
- [x] Provider mapping 测试只断言各自 wire schema；Context 测试只断言公共 IR。

退出条件：两个 Provider 都只通过新 IR 构造 wire payload，旧 role-based input 不再用于生产路径。

## 3. Session 收拢对话事实与持久化

- [x] 将 `ConversationMessage` 重命名或扩展为 `ConversationEntry`。
- [x] 将 `ToolResultsMessage` 迁移为非角色化 `ToolResultBatch`。
- [x] 保持 `HumanMessage` 是唯一 turn 起点。
- [x] 保持每个完整模型响应生成一个 `AssistantMessage`，即一个已完成 step 的事实。
- [x] 将 `Conversation` 变为 `Session` 私有聚合实现。
- [x] 按消费者拆分为 `conversation`、`ContextPlanningState` 与 `AttachmentDerivationState`。
- [x] 让所有写入通过 Session 语义提交方法。
- [x] 隐藏 `SessionStore`、records、codec 和 JSONL 路径。
- [x] 保持 persistence-first、memory-after-success 原子性。
- [x] 为旧 JSONL `tool_results_message` record 提供向后读取兼容；新写入使用 `tool_result_batch`。
- [x] 迁移 session catalog，使其只返回 summary DTO。

退出条件：生产代码中只有 Session 能修改 conversation entries；不存在平级的可写 Conversation 入口。

## 4. 收拢 Session 级关联状态

### Context state

- [x] 以 `AttachmentMessage` 取代 attachment/Todo delivery sidecar，并由 Session 统一排序与持久化策略。
- [x] 将 session prompt cache 和 user-context cache 移入 Session。
- [x] 将 runtime-stable prompt section 改为 bootstrap 时生成的不可变 snapshot，删除 `PromptRegistry` 的隐式 runtime cache。
- [x] 删除或降级 `ContextSession`；ContextEngine 不再持有 session 生命周期状态。
- [x] 明确 resume/switch 后 durable Attachment 恢复和 transient Attachment 重新派生规则。

### Tool result state

- [x] 将 `ToolResultStore` 的 session binding 移入 Session 私有持久化。
- [x] ToolExecutor 返回完整领域结果，不持有活动 session 目录。
- [x] Session 提交负责外置大结果、record 和 presentation snapshot 的一致性。
- [x] 保持取消与异常时全部 ToolCall 被闭合。

### Provider replay

- [x] 从 Conversation content 移除 `ProviderContinuationState` 字段。
- [x] 增加按 entry/content ID 关联的 `ProviderReplayRecord`。
- [x] 将 OpenAI response item 和 Anthropic thinking payload 迁入 sidecar。
- [x] 保持 binding 匹配、active-trajectory/working-context scope 和 compact 裁剪。
- [x] 恢复旧 transcript 时把旧 continuation record 投影到新 sidecar。

退出条件：切换 Session 时只需替换一个 Session 引用即可同时切换 messages、context state、工具结果和 replay state。

## 5. 引入 AppState

- [x] 定义 `AppState` 和 `WorkspaceState`、`PermissionState`、`ProviderRuntime` 边界。
- [x] 将 `ChatService._active` session bundle 移入 AppState。
- [x] 将 `ActiveModelState` 合并到 ProviderRuntime。
- [x] 将 runtime permission mode/rules 的唯一引用移入 PermissionState。
- [x] 将 session/provider switch 互斥与原子替换移入明确 application operation。
- [x] ChatService 改为持有 AppState 并协调用例，不再拥有重复状态字段。
- [x] AgentEngine、ContextEngine、ToolExecutor 和 Provider adapter 不得持有 AppState。
- [x] CLI/TUI 只通过 application service 读取安全的状态 view。
- [x] runtime close 明确关闭 Provider client 和 pending host operation。

退出条件：runtime 活动状态可以从一个 AppState 图完整解释；ChatService 不再是隐式状态容器。

## 6. 删除兼容路径并增加架构守卫

- [x] 删除 `ModelUserMessage`、`ModelAssistantMessage` 和 `ModelToolResultBlock` 的旧生产入口。
- [x] 删除公开可变 `Conversation` API。
- [x] 删除 `ContextSession` 或遗留 session bundle。
- [x] 删除 ToolExecutor 的活动 result-store binding。
- [x] 更新 AST 依赖允许表。
- [x] 增加守卫：Provider SDK 类型只能出现在 `providers`。
- [x] 增加守卫：JSONL records/store 只能出现在 `sessions` 私有实现。
- [x] 增加守卫：除 application service/bootstrap/host 边界外不得依赖完整 AppState。
- [x] 增加守卫：除 Session 外不得保存或修改 canonical conversation collection。
- [x] 搜索并清理 `ToolResultsMessage`、`ModelMessage.messages` 等旧术语。

退出条件：没有双重 API、兼容转发层或临时架构例外。

## 7. 最终同步与验证

- [x] 更新 `docs/README.md` 架构图和统一术语。
- [x] 更新 `docs/01-agent-loop.md` 的多 step turn 流程。
- [x] 更新 messages、sessions、context、providers、permissions 和 package boundary 文档。
- [x] 把本目录从“提案”标记为“已实现”，或将最终内容合并到顶层当前实现文档。
- [x] 运行 `uv run ruff format .`。
- [x] 运行 `uv run ruff check .`。
- [x] 运行 `uv run pyright`。
- [x] 运行 `uv run pytest`。
- [x] 逐项关闭 [04-acceptance-criteria.md](04-acceptance-criteria.md) 中的所有验收项。

退出条件：代码、测试、架构守卫和当前实现文档对状态所有权给出同一个答案。
