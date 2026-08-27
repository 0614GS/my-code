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
    "cli": {"auth", "chat", "config", "permissions"},
    "tui": {
        "chat",
        "config",
        "features.file_mentions",
        "features.todos",
        "model",
        "permissions",
        "providers",
        "sessions",
        "tools",
    },
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
- `agent` 不得导入 OpenAI、Anthropic、prompt_toolkit、Rich、CLI 或 JSONL record。
- `agent` 不得负责 session catalog、resume 或保存活动 Session 单例。
- `providers` 不得导入 Agent、Context、Tools、Chat 或 UI。
- `tui` 不得直接导入 Agent、Provider SDK、SessionStore、ToolExecutor、Conversation 或 Context；它可以从公开语义模块读取安全展示 DTO。
- 除 `bootstrap` 外，任何模块不得注册全部工具或创建完整运行时对象图。

## 公开 API

顶层领域包的 `__init__.py` 不聚合 API。每个语义子模块使用静态 `__all__` 声明自己的稳定能力：

```python
from my_code.model.client import ModelClient
from my_code.model.request import ModelRequest
from my_code.conversation.models import ToolCall
from my_code.conversation.state import Conversation
```

跨模块必须直接指向符号所有者：

```python
# 禁止：包根聚合
from my_code.model import ModelRequest

# 禁止：Chat 转发其他所有者
from my_code.chat import PermissionConfirmation

# 允许：语义子模块声明自身能力
from my_code.model.request import ModelRequest
from my_code.permissions.models import PermissionConfirmation
```

模块内部可以直接导入自己的实现文件。跨模块不能导入以下划线开头的私有路径、使用 wildcard，或引用未列入目标模块 `__all__` 的符号。公开模块不得 re-export 其他架构模块拥有的能力。
