# Session 事实与模型输入

## Turn、Step 与事实顺序

本项目固定使用以下定义：

- turn 由真实用户输入划分；
- step 是一次模型调用；
- 一个 turn 可以包含多个 step；
- 一个 step 得到完整响应后生成一个 `AssistantMessage`；
- 一个包含工具调用的 `AssistantMessage` 后面必须有一个闭合的 `ToolResultBatch`，然后才可以开始同一 turn 的下一个 step。

```text
Turn N
├── HumanMessage
├── Step 1
│   ├── AssistantMessage(text + tool calls)
│   └── ToolResultBatch
├── Step 2
│   ├── AssistantMessage(tool calls)
│   └── ToolResultBatch
└── Step 3
    └── AssistantMessage(final text)
```

`AssistantMessage` 不是 turn。运行期可以有一个失败的 step 尝试而没有对应 `AssistantMessage`；流式 delta 也不会在完整响应前成为 Session 事实。

## Session 内部数据分层

Session 持有全部 session-scoped 模型无关数据，但需要区分持久事实和临时输入：

```python
@dataclass(frozen=True, slots=True)
class SessionSnapshot:
    conversation: tuple[ConversationEntry, ...]
    working_set: tuple[ConversationEntry, ...]
    replacements: tuple[ContentReplacement, ...]
    replay_records: tuple[ProviderReplayRecord, ...]
```

### ConversationEntry

```python
type ConversationEntry = (
    HumanMessage | AssistantMessage | ToolResultBatch | ConversationSummary
    | AttachmentMessage
)
```

- `HumanMessage`：真实用户输入，开始一个 turn。
- `AssistantMessage`：一次成功完成的 step 输出，可包含 text、reasoning 和 tool calls。
- `ToolResultBatch`：一个 assistant message 发起的工具调用结果，不属于 human 或 assistant role。
- `ConversationSummary`：full compact 产生的可恢复事实和 working-set 锚点。
- `AttachmentMessage`：原位结构化辅助上下文；Context 只把它投影为 `UserInput`。

当前实现使用 `ToolResultBatch`；名称和领域归属都不包含 user/human role。

Attachment payload 不声明 retention。Session 按类型持久化文件上下文、Skill 正文、invoked skills 与后台结果；Skill listing 和 Todo reminder 仅进入内存 Conversation。非持久化 Attachment 不成为 durable entry 的父节点，因此 transcript 恢复不会断链。prompt/user-context cache 仍是 Session 内部非 Conversation 状态。

## Provider replay sidecar

canonical message 内容不得直接携带 OpenAI response item、Anthropic thinking signature 或 SDK object。需要跨 step/resume 重放的数据单独保存：

```python
@dataclass(frozen=True, slots=True)
class ProviderReplayRecord:
    entry_id: str
    content_id: str
    binding: ProviderBinding
    scope: ReplayScope
    payload: JsonObject
```

约束：

- replay record 必须关联一个已提交的 Session entry/content；
- 只有 binding 匹配的 Provider adapter 可以消费 payload；
- provider 切换保留 canonical facts，但不向新 provider 发送旧 payload；
- compact 移除工作集内容时，同步裁剪不再需要的 replay record；
- payload 只由对应 adapter 编码和校验，其他模块视为 opaque；
- replay 是请求连续性的补充，不能替代完整 Session conversation。

OpenAI 的 `response item id` 与内部 tool `call_id` 不应混为一谈。跨工具调用配对使用 provider-neutral `call_id`；response item id 留在 replay payload。

## ModelRequest 使用有序输入项

目标 API：

```python
@dataclass(frozen=True, slots=True)
class ModelRequest:
    instructions: SystemPrompt
    input: tuple[ModelInputItem, ...]
    tools: tuple[ModelToolDefinition, ...]
    max_output_tokens: int


type ModelInputItem = UserInput | AssistantOutput | ToolOutputs
```

### UserInput

```python
@dataclass(frozen=True, slots=True)
class UserInput:
    content: tuple[InputContent, ...]


type InputContent = InputText | InputImage | InputDocument
```

它可以来自真实 `HumanMessage`，也可以来自 ContextEngine 注入的 user context、attachment、summary 或 reminder。来源信息用于调试、预算和投影，不要求写入 wire payload。

### AssistantOutput

```python
@dataclass(frozen=True, slots=True)
class AssistantOutput:
    content: tuple[AssistantOutputContent, ...]


type AssistantOutputContent = OutputText | ReasoningItem | ToolCall
```

它对应一个已完成 step 的模型输出，并保持 provider 返回的语义顺序。匹配的 replay sidecar 可以附在 request item 的只读投影视图中，但不能写入 canonical `AssistantMessage` 字段。

### ToolOutputs

```python
@dataclass(frozen=True, slots=True)
class ToolOutputs:
    results: tuple[ToolOutput, ...]


@dataclass(frozen=True, slots=True)
class ToolOutput:
    call_id: str
    content: tuple[ToolOutputContent, ...]
    is_error: bool = False
```

`ToolOutputs` 与 `UserInput`、`AssistantOutput` 并列。不得把它包装成 `ModelUserMessage` 以满足某个 Provider 的 schema。

公共模型支持 text、image 和 document tool output，使用 provider-neutral content block；Provider SDK 类型不会进入 `model`。

## Provider 映射

| Model input | Anthropic Messages | OpenAI Responses |
| --- | --- | --- |
| `UserInput` | user message content | user message input item |
| `AssistantOutput.OutputText` | assistant text block | assistant message/output item |
| `AssistantOutput.ReasoningItem` | thinking/redacted-thinking block | reasoning item 或匹配 replay item |
| `AssistantOutput.ToolCall` | assistant tool_use block | function_call item |
| `ToolOutputs` | user message 中的 tool_result blocks | 顶层 function_call_output items |
| `InputImage` | image block | input_image |
| `InputDocument` | document block | input_file/file content |
| `SystemPrompt` | system 参数/blocks | instructions 或明确的 system/developer input |

Anthropic adapter 负责：

- 将 `ToolOutputs` 包装为 user-role tool_result blocks；
- 按 Anthropic 约束合并相邻同 role 输入；
- 保持 assistant thinking/text/tool_use block 顺序；
- 映射 `is_error`；
- 只重放 binding 匹配的 thinking/redacted-thinking payload。

OpenAI Responses adapter 负责：

- 将 assistant output 展开为 message、reasoning 和 function_call items；
- 将每个 `ToolOutput` 展开为独立 function_call_output；
- 使用 `call_id` 配对调用和结果；
- 对不支持独立 `is_error` 的 function output 使用稳定的文本或结构化编码；
- 只重放 binding 匹配的 response output item。

相邻 role 合并属于 Provider wire normalization，不属于 ContextEngine。ContextEngine 保留输入项的语义边界和顺序。

## ContextEngine 边界

```python
class ContextEngine:
    def plan(
        self,
        session: SessionSnapshot,
        request: RequestContext,
        model: ModelEnvironment,
    ) -> ContextPlan: ...
```

ContextEngine 负责：

- 选择 Session working set；
- 应用 content replacement；
- 投影 ConversationEntry 为 ModelInputItem；
- 将 Conversation 中的 Attachment 原位投影为标准 UserInput，并插入 user context；
- 选择可用于当前 binding 的 replay sidecar；
- 校验 tool call/result 配对；
- 预算、microcompact 和 full compact proposal；
- 输出一次性 `ModelRequest`。

ContextEngine 不负责：

- 保存或修改 Session entries；
- 决定 Session 的持久化格式；
- 运行动态 attachment source 或持有其状态；
- 合并 Anthropic role message；
- 生成 OpenAI/Anthropic SDK 类型。

## 必须保持的序列不变量

- `HumanMessage` 开始一个新 turn。
- `AssistantMessage` 对应一个完整成功的 step；一个 turn 可有多个。
- 工具执行前必须先提交包含 ToolCall 的 `AssistantMessage`。
- `ToolResultBatch.source_assistant_id` 必须引用直接相关的 assistant message。
- batch 必须恰好闭合该 assistant message 中的全部 ToolCall。
- unresolved ToolCall 后不能开始下一 step。
- Context 注入项不能破坏 tool call 与 tool output 的协议邻接要求；必要的 wire 邻接由 adapter 完成。
- Provider 流式事件、部分文本和未完成 tool arguments 不成为 Session 事实。
- full compact 不能保留孤立 ToolOutput 或删除其对应 ToolCall。
