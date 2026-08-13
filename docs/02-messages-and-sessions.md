# 消息与会话

## 1. 内部消息不是 API 消息

运行时使用以 `type` 为判别字段的 `Message` 联合类型，定义在 `claude-code/src/types/message.ts`。主要成员如下：

| 类型 | 用途 |
| --- | --- |
| `assistant` | 模型返回的 text、thinking、tool_use 等内容块 |
| `user` | 人类输入、工具结果、合成提醒、compact summary |
| `system` | API 错误、compact boundary、耗时等控制或展示信息 |
| `attachment` | 文件变化、记忆、计划、hook 上下文等延迟注入内容 |
| `progress` | 工具和 hook 的瞬时进度 |
| `tombstone` | 删除失败流式尝试产生的孤立消息 |

这里的 `user` 表示 Anthropic 对话协议中的 user role，并不等于“人类亲手输入”。`tool_result`、系统生成的提醒和 compact summary 也通过 user role 进入模型上下文。

## 2. 三种消息视图

同一会话存在三个用途不同的视图：

```text
内部 Message[]
  ├─ UI / SDK：保留 progress、system、attachment 等事件
  ├─ API 投影：只留下合法的 user / assistant 序列
  └─ Transcript：只保存恢复会话需要的数据与元数据
```

`normalizeMessagesForAPI()` 负责 API 投影，入口为 `claude-code/src/utils/messages.ts:1989`。它会：

- 调整 attachment 的位置，避免破坏工具调用顺序。
- 移除 display-only 的 virtual 消息、progress 和大多数 system 消息。
- 把需要模型看到的本地命令或 attachment 转成 user 内容。
- 过滤无效或已经不可用的 `tool_reference`。
- 合并连续 user 消息，兼容不接受相邻同 role 消息的 provider。
- 合并同一次模型响应拆出的 assistant block。
- 保持 `tool_use` 与对应 `tool_result` 的协议顺序。

因此，UI 需要什么、磁盘保存什么、模型看到什么，不能由同一个 `list` 的简单切片决定。

## 3. 四类 ID

源码同时使用几种不同身份：

- `Message.uuid`：本地消息记录 ID，用于 Transcript 链和 UI 更新。
- `assistant.message.id`：模型响应 ID；一次流式响应拆出的多个 assistant 记录共享它。
- `tool_use.id`：工具协议 ID，`tool_result.tool_use_id` 必须与之相同。
- `sourceToolAssistantUUID`：工具结果指向产生该调用的本地 assistant UUID。

不能用 `message.id` 替代 `uuid`：并行工具调用时，一个 API 响应可能被拆成多个本地消息。也不能用 assistant UUID 替代 tool ID：一个 assistant block 可以包含多个工具调用。

## 4. JSONL Transcript

主会话按项目目录和 session ID 写入 JSONL；子 Agent 写入独立 sidechain 文件。核心实现位于 `claude-code/src/utils/sessionStorage.ts`。nano-code 的对应磁盘布局见 [09-storage-and-settings.md](09-storage-and-settings.md)。

Transcript 的正常写入路径是追加式记录，除消息外还可包含：

- title、tag、mode、worktree 等会话元数据；
- file history 和 attribution snapshot；
- tool result content-replacement 决策；
- queue operation 和 compact/collapse 元数据。

首次 user/assistant 消息出现前，写入可以暂存；一旦用户输入被接收，`QueryEngine` 会在模型响应前持久化它。

流式 fallback 产生 tombstone 时存在一个有意的例外：存储层会定点移除失败尝试留下的孤立消息；目标通常位于文件尾部，超出尾部窗口时才退化为受大小限制的重写。

高频 `progress` 是瞬时 UI 状态，不写入新 Transcript，也不参与父链。加载旧版本日志时，读取器会跨过历史上误写入链中的 progress 节点。

## 5. 父链、分支与恢复

普通 Transcript 消息包含 `parentUuid`，恢复时从叶子向父节点回溯，再反转成对话顺序。

写入规则的关键点：

- 新消息默认以最近的 chain participant 为父节点。
- tool result 优先以 `sourceToolAssistantUUID` 为父节点。
- compact boundary 的 `parentUuid` 置空，表示新物理链起点；旧父节点写入 `logicalParentUuid` 供展示和诊断。
- 重复 UUID 不再次写入，防止每轮把完整内存数组重复追加。

这使 rewind、fork 和 sidechain 可以共享已有消息，而不复制成一条完全线性的日志。

## 6. 并行工具带来的 DAG

父链接近链表，但并行工具会形成 DAG：同一个模型响应的多个 assistant 片段拥有相同 `message.id`，各自的 tool result 又指向不同 assistant UUID。单纯沿一条 `parentUuid` 回溯可能丢掉兄弟分支。

`buildConversationChain()` 后会运行恢复遍历：按共享的 API message ID 收集离链 assistant 片段，再找出它们的 tool result，按时间顺序插回响应组。源码入口：

- `claude-code/src/utils/sessionStorage.ts:2040`
- `recoverOrphanedParallelToolResults()`

这说明持久化模型必须显式支持“一次响应有多个并列内容块”，不能假设所有事件天然是一条链。

## 7. Compact 后的恢复

compact boundary 表示模型上下文从这里重新开始，但 Transcript 仍可能保存被摘要的旧记录。加载时会：

1. 定位最后一个 boundary。
2. 删除 boundary 之前不再需要的消息。
3. 如果 compact 保留了一段原消息，依据 `preservedSegment` 把该段首尾重新接到摘要链上。
4. 清零保留 assistant 消息里的旧 usage，防止恢复后立即再次触发 compact。

如果 preserved segment 不完整，读取器宁可放弃裁剪、恢复更多旧历史，也不构造一条断链会话。

## 8. 核心不变量

1. API 投影、UI 事件和持久化记录是三个独立边界。
2. progress 不得成为可恢复对话的父节点。
3. 每个 tool result 必须能定位到 tool use 和产生它的 assistant 消息。
4. Transcript 写入必须按 UUID 幂等。
5. 恢复逻辑必须检测环、断链和并行工具的兄弟分支。

## 9. 主要源码入口

- `claude-code/src/types/message.ts`
- `claude-code/src/utils/messages.ts`
- `claude-code/src/utils/sessionStorage.ts`
- `claude-code/src/utils/sessionRestore.ts`
- `claude-code/src/assistant/sessionHistory.ts`

## 10. nano-code 的会话发现与恢复边界

nano-code 将“列出会话”和“恢复会话”分开处理：

1. `SessionCatalog` 只扫描当前项目对应的状态目录，通过 `stat` 和有界文件头提取首条用户提示，不为列表加载完整 JSONL。
2. 候选按文件修改时间倒序排列，当前活动会话、空文件、异常首记录、非 UUID 文件和符号链接都会被过滤。
3. 用户选择后，`SessionStore` 才严格解析完整记录，校验 UUID 唯一性和父节点顺序，并从最后一条记录沿父指针恢复活动分支。
4. `AgentEngine.resume()` 先完成目标会话校验及未闭合工具轮修复，再替换内存状态；失败时保留原会话。
5. runtime 在同一临界区切换 transcript、消息历史和工具结果目录，TUI 只接收展示 DTO，不接触 `ChatMessage` 或 JSONL。

这对应 Claude Code 的轻量日志列表、选中后完整加载、集中恢复 session-scoped 状态三层设计。nano-code 当前没有 compact、fork 和并行工具兄弟分支，因此活动链算法仍保持更小的范围。
