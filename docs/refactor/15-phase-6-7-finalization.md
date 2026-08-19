# 阶段 6–7：Agent、Chat 与收尾

> 本文记录阶段 6–7 完成时的快照。后续公开 API 与 dataclass 收敛见 `16-semantic-api-dataclass-review.md`；其中的文件数量、import 数量和“只从包根导入”结论不再代表当前实现。

## 最终边界

- `AgentEngine` 只拥有模型/工具循环算法与不可变依赖。每次 `submit/stream` 显式接收 `Session`、`ContextSession` 和 `ToolResultStore`，不提供 status、resume 或 session catalog。
- `ChatService` 是唯一活动会话协调者。它持有一个 `Session + ContextSession + ToolResultStore` bundle，并用同一把锁串行化 submit、stream、compact、resume 和 provider switch。
- `sessions` 完成目标 Session 的严格 hydration、尾部工具轮修复与 catalog；Chat 只在候选 bundle 完整构造和历史投影成功后替换活动引用。
- `chat` 拥有 host DTO 和 AgentEvent → ChatEvent 投影。阶段完成时 TUI 通过 `my_code.chat` 聚合入口导入；该入口随后改为语义子模块 API。
- `config` 拥有路径、settings、provider profile schema/store 和权限更新的持久化协调；根 `bootstrap.py` 是唯一完整对象图与进程入口。

`AgentInboundPort`、Agent contracts/ports、`ChatRuntime` Protocol、`DefaultChatRuntime`、CLI application adapters、`core`、`constants`、`application` 以及未实现的空 `hooks`/`mcp` 包均已删除，没有旧路径 re-export。

## 状态与失败语义

```text
runtime: ChatService + provider/policy/registry
session: SessionBundle(Session, ContextSession, ToolResultStore)
turn:    Agent step/usage/stream/tool-round locals
request: ModelRequest/provider stream locals
```

- Tool result store 不再通过 Executor 的 mutable session binding 切换，而是随 turn 显式传递。
- resume 目标为空、损坏、修复写入失败或历史投影失败时，旧 session ID、Conversation、ContextSession delivery/cache 与 ToolResultStore 全部保持不变。
- 成功 resume 创建全新的 ContextSession 和目标 session 工具结果目录，运行时 provider、permission 与 workspace 不从 Transcript 覆盖。
- 流式 turn 未结束时 resume 等待同一互斥边界；不存在输出途中切换 JSONL 或工具结果归属的窗口。

## 前序阶段复查

- Conversation 仍是内存 canonical facts 的唯一所有者；Session 仍执行先落盘后更新内存。
- ContextBuilder 仍不缓存会话 history；ContextSession 的 session cache 随 bundle 替换销毁。
- Agent、Context、Conversation、Model、Permissions、Sessions、Tools 与 Workspace 均不 import 具体 feature。
- Todo 仍从完整 Conversation history 投影；File mention 仍只产生 Context attachment。
- Permission 拒绝/取消、工具异常/取消、compact 失败、provider binding 和 resume 修复测试全部保留。

## 架构结果

- 生产 Python 文件：120。
- 跨模块 import 记录：158。
- 临时依赖例外：0。
- 临时深层 import 例外：0。
- 技术泄漏例外：0。
- 生产模块依赖环：0。

AST 守卫不再包含旧 `core.bootstrap` 或 `application.chat` 的特殊映射。本文完成时仍采用模块根公开 API；后续已改为由语义子模块静态声明 API。

## 验证

- 新增 session bundle 成功切换、失败不变和 stream/resume 互斥测试。
- Ruff format/check、Pyright standard、完整 pytest、AST 依赖/公开 API/技术泄漏守卫和 CLI `--help` smoke 全部通过。
