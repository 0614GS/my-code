# 阶段 0：行为基线与冻结决策

本文是迁移阶段 0 的决策记录。后续阶段不得自行改变这里的数据语义；如需改变，必须先更新本文、迁移计划和对应 characterization test。

## 术语与行为基线

- **turn**：从一次真实 `HumanMessage` 开始，到下一次真实 `HumanMessage` 之前结束。
- **step**：一次模型调用。只有完整、成功的模型输出才提交一个 `AssistantMessage`；失败或被取消的调用尝试不是已完成 step 事实。
- **tool round**：闭合一个 `AssistantMessage` 发起的全部 `ToolCall` 的执行过程，完成后只提交一个结果批次。

一个包含工具调用的两 step turn 的持久事实顺序固定为：

```text
HumanMessage
AssistantMessage       # step 1
ToolResultBatch        # step 1 发起的一个 tool round
AssistantMessage       # step 2
```

基线证据见 `tests/unit/test_agent_engine.py::test_one_human_turn_can_contain_multiple_steps_and_one_tool_round`。取消的 tool round 仍闭合全部 call，见 `tests/unit/test_tool_round_executor.py::test_round_executor_cancellation_closes_every_call`。

## Provider wire 基线

公共层当前暂时把工具结果包装在 `ModelUserMessage`，这是待迁移的建模偏差，不是领域语义。

- OpenAI Responses 输出顶层 `function_call`，随后输出顶层 `function_call_output`；结果不编码成 user message。
- Anthropic Messages 输出 assistant-role `tool_use`，随后输出 user-role `tool_result`；user role 只属于 Anthropic adapter 的 wire 编码。

快照证据分别见：

- `tests/unit/test_openai_responses_provider.py::test_openai_tool_sequence_snapshot_uses_top_level_function_items`
- `tests/unit/test_anthropic_provider.py::test_anthropic_tool_sequence_snapshot_uses_assistant_then_user_roles`

## Provider continuation 基线

- `ProviderBinding` 匹配时，`working_context` continuation 可在工作上下文中重放。
- `active_trajectory` continuation 仅在直接工具续接轨迹中重放。
- binding 不匹配时不重放 provider payload，但保留 provider-neutral 的可见事实。
- compaction 输入剥离全部 continuation；compact 后被工作集裁掉的旧内容不再参与请求。

证据见 `tests/unit/test_reasoning_models.py::test_scoped_continuations_are_selected_and_compaction_strips_them`，两个 adapter 的匹配/不匹配测试负责锁定各自 payload 过滤。

## Session 失败基线

- 写入采用 persistence-first；写失败时内存 history、working set、replacement 和 compact boundary 不变。
- resume 必须先完整加载、校验并修复候选 Session；空 Session 或修复写入失败时，当前 Session 引用不变。
- tool round 取消时，为尚未完成的 call 生成 error result，先提交闭合批次，再向调用方传播取消。

证据见 `tests/unit/test_conversation_state.py` 中的 persistence、resume 与 compaction 失败测试，以及 `tests/unit/test_tool_round_executor.py` 和 `tests/unit/test_agent_engine.py` 中的取消测试。

## 冻结决策

### D0-1：结果批次命名为 `ToolResultBatch`

采用 `ToolResultBatch`，不采用 `ToolResultsEntry`。`Batch` 强调它恰好闭合来源 `AssistantMessage` 的一次 tool round；它是 `ConversationEntry` 的一个变体，但不需要把 `Entry` 重复进每个类名。字段统一使用 `source_assistant_id`；迁移期间旧 `source_assistant_uuid` 只作为持久化兼容输入。

### D0-2：`AppState` 位于 `my_code.application.state`

新增 `application/` 作为用例编排边界，最终路径为 `src/my_code/application/state.py`。不得放到顶层通用 `state.py`，不得提供进程全局 singleton 或 `get_app_state()`。现有 `chat/` 可以在迁移期调用 application 层，完成后只保留面向交互产品的 facade/projection。

### D0-3：显式用户附件持久化不可变内容快照

显式用户附件作为该次 `HumanMessage` 的内容事实持久化，至少包含：稳定 attachment/content ID、提交时解析后的 provider-neutral 内容、来源显示名和可选 workspace-relative source reference。

- source reference 只用于展示、审计和显式刷新，不是恢复所需数据；不得持久化 workspace 外绝对路径。
- resume 使用提交时的内容快照，不重新读取源文件，避免内容随文件变化而漂移。
- 源文件缺失、移动或变更不阻止恢复；仍使用快照，并可向 UI 暴露“来源当前不可用/已变化”的诊断。
- 若旧记录只有引用而没有快照，恢复时尝试在当前 workspace 安全边界内读取一次并立即升级为快照；不可读取时保留 Human 文本事实和缺失附件占位，不静默丢弃整个消息。
- live-session attachment delivery 仍是派生投递状态，默认不持久化为 canonical entry。

### D0-4：replay sidecar 使用独立 JSONL schema v1

sidecar record 初始版本固定为 `schema_version: 1`，record type 为 `provider_replay`，必需字段为 `entry_id`、`content_id`、`binding`、`scope` 和 JSON `payload`。它与 transcript schema 独立演进，写入只产生当前 v1。

兼容策略：

- 读取现有 transcript v5 内嵌 continuation，并在内存中投影为 replay record；首次发生 Session 语义写入时可增量写入 v1 sidecar，但不得就地改写旧 transcript。
- 同一 `(entry_id, content_id, binding, scope)` 同时存在 sidecar 与 legacy payload 时，v1 sidecar 优先；内容冲突应报告恢复错误，不静默选择。
- 未知的未来 sidecar schema version 必须 fail closed，并保持当前活动 Session 不变。
- payload 由对应 provider adapter 校验；Session codec 只验证其为 JSON object 和关联键完整。
- compact 提交必须把 summary/boundary 与 replay 裁剪作为一个 Session 事务；不得留下指向已移出 working set 且 scope 不再需要的 replay record。

## 阶段 0 退出检查

阶段 0 只冻结行为和语义，不引入新生产类型。`ToolResultBatch`、`application.state`、附件内容模型和 replay sidecar record 均从各自后续阶段开始实现。
