# 模块依赖与公开 API

## 依赖顺序

下文统一使用 `A -> B` 表示“A 可以 import B”。生产代码按以下顺序组合，后面的层可以依赖前面的层：

```text
L0  model  workspace
L1  conversation  permissions  prompts  auth
L2  config  context  tools
L3  providers  sessions  features.todos  features.file_mentions
L4  agent
L5  features.subagents  features.plan_mode
L6  chat
L7  cli  tui
L8  bootstrap
```

层级只帮助阅读，不自动授权同层或所有低层依赖。AST 守卫以下面的允许表为唯一机器标准。

## 最终允许表

```python
ALLOWED_DEPENDENCIES = {
    "model": set(),
    "workspace": set(),
    "permissions": {"model"},
    "prompts": {"model"},
    "auth": {"model"},
    "config": {"auth", "model", "permissions"},
    "conversation": {"model"},
    "context": {"conversation", "model", "prompts"},
    "tools": {"conversation", "model", "permissions", "workspace"},
    "sessions": {"conversation", "model", "tools"},
    "providers": {"auth", "config", "model"},
    "agent": {"context", "conversation", "model", "sessions", "tools"},
    "features.file_mentions": {"context", "permissions", "tools", "workspace"},
    "features.todos": {"context", "conversation", "model", "permissions", "tools"},
    "features.subagents": {"agent", "context", "tools"},
    "features.plan_mode": {"agent", "prompts", "tools"},
    "chat": {
        "agent",
        "config",
        "context",
        "conversation",
        "features.file_mentions",
        "features.todos",
        "model",
        "permissions",
        "providers",
        "sessions",
        "tools",
    },
    "cli": {"auth", "chat", "config"},
    "tui": {"chat"},
}
```

该表是最终机器规则。实现阶段如果发现真实职责不符，应先修改模块设计文档，再调整允许表；不能只为让测试通过而增加依赖。

`bootstrap` 是组合根，可以依赖所有生产模块，但任何模块不得依赖 `bootstrap`。

## 明确禁止

- `model` 不得导入 provider SDK、Agent、Session 或 UI。
- `conversation` 不得导入 Context、Tools、Session、Chat 或 UI。
- `context` 不得导入具体 Provider、ToolExecutor、Session 或 UI。
- `permissions` 不得导入 Tool、Workspace 实现或 UI。
- `workspace` 不得导入 PermissionRule。
- `agent` 不得导入 OpenAI、Anthropic、Textual、CLI 或 JSONL record。
- `agent` 不得负责 session catalog、resume 或保存活动 Session 单例。
- `providers` 不得导入 Agent、Context、Tools、Chat 或 UI。
- `tui` 不得直接导入 Agent、Provider、SessionStore、ToolExecutor 或 Context。
- 除 `bootstrap` 外，任何模块不得注册全部工具或创建完整运行时对象图。

## 公开 API

每个模块使用 `__init__.py` 和 `__all__` 声明稳定能力：

```python
from nano_code.model import ModelClient, ModelRequest
from nano_code.conversation import Conversation, ToolCall
```

跨模块默认不得深层导入：

```python
# 禁止
from nano_code.model.request import ModelRequest

# 允许
from nano_code.model import ModelRequest
```

模块内部可以直接导入自己的子模块。最终生产代码没有深层 import 例外；公开 re-export 只声明当前能力，不兼容旧路径。
