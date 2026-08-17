# 权限系统

## 1. 权限不是布尔值

工具自身先返回 `PermissionResult`：

```text
allow | ask | deny | passthrough
```

进入统一权限层后收敛为最终 `PermissionDecision`：

```text
allow | ask | deny
```

决策除了 behavior，还可以包含：

- `updatedInput`：hook 或用户确认后修改过的工具输入；
- `message`：询问或拒绝原因；
- `suggestions`：可写入设置的权限更新；
- `decisionReason`：规则、mode、hook、安全检查、classifier 等来源；
- `contentBlocks`：用户确认时附带的文本或图片反馈。

类型定义位于 `claude-code/src/types/permissions.ts`。

## 2. Permission Context

每个会话维护 `ToolPermissionContext`：

- 当前 `mode`；
- allow、deny、ask 三组规则，且保留规则来源；
- additional working directories；
- bypass/auto mode 是否可用；
- 是否无法显示交互式权限框；
- plan mode 之前的 mode。

权限更新返回新 context；持久化只允许写入 user、project、local 等可编辑设置源。managed policy 可以启用“只接受托管权限规则”，此时用户侧的 always-allow 选项被禁用。

## 3. 规则表示

规则字符串有两种基本形式：

```text
ToolName
ToolName(content)
```

示例：

```text
Bash
Bash(git status)
Bash(git:*)
Bash(git *)
Read(./src/**)
mcp__github
```

- 只有工具名表示整个工具。
- `Tool(*)` 和 `Tool()` 会规范化为整个工具规则。
- shell content 支持 exact、旧式 `:*` prefix 和 `*` wildcard。
- 括号和反斜杠可转义，规则持久化前会规范化。
- MCP 的 server 级规则可匹配该 server 下全部工具。
- 旧工具名在解析时转换成当前 canonical name。

规则来源包括 user、project、local、flag、managed settings，以及 cliArg、command 和 session。来源用于持久化、展示与审计；真正的 behavior 优先级由决策管线明确规定。

实现位于 `claude-code/src/utils/permissions/permissionRuleParser.ts` 和 `permissionsLoader.ts`。

## 4. 核心决策顺序

`hasPermissionsToUseToolInner()` 的顺序是权限语义的核心：

```text
0. 已取消 → Abort
1. 整个工具的 deny 规则
2. 整个工具的 ask 规则
3. tool.checkPermissions(input, context)
4. 工具自身 deny
5. 必须交互的工具所返回的 ask
6. content-specific ask 规则
7. bypass 也不能跳过的 safety check
8. bypassPermissions mode
9. 整个工具的 allow 规则
10. passthrough 转为 ask
```

因此：

- blanket deny 先于所有 allow 和 mode。
- 用户显式配置的 ask 不会被 bypass 静默跳过。
- 敏感路径等 safety check 对 bypass 免疫。
- blanket allow 不能覆盖前面已经命中的 deny/ask。
- tool-specific permission 可以返回修改后的输入，后续执行必须使用最终输入。

入口为 `claude-code/src/utils/permissions/permissions.ts:1158`。

## 5. Mode 只改变默认策略

外部可用 mode 包括：

| Mode | 主要语义 |
| --- | --- |
| `default` | 未被规则或工具自动允许的操作进入 ask |
| `acceptEdits` | 工具可自动允许工作区内通过安全检查的编辑 |
| `plan` | 由各工具限制为规划阶段允许的操作，并保留进入前 mode |
| `dontAsk` | 最终的 ask 统一转成 deny |
| `bypassPermissions` | 跳过普通询问，但仍尊重前置 deny、ask、交互和 safety check |

`auto` 和 `bubble` 是内部 mode；`auto` 可用分类器代替人工询问，但分类器不可用或安全检查不可批准时仍需 fail closed 或回退交互。

Mode 不应直接写进 Agent Loop。它只是权限引擎和工具特定检查的输入。

## 6. Hook 与权限的优先级

PreToolUse hook 可以 allow、ask、deny 或修改输入，但 hook allow 仍要重新执行规则级检查：

- deny 规则覆盖 hook allow；
- ask 规则仍要求确认；
- requires-user-interaction 工具不能被普通 hook allow 静默跳过；
- hook deny 直接终止该工具；
- hook 的 `updatedInput` 要重新进入后续权限判断。

统一实现位于 `resolveHookPermissionDecision()`。其他 hook 行为见 [07-hooks.md](07-hooks.md)。

## 7. 路径权限

文件权限不是简单的字符串 `startswith(cwd)`。检查过程包括：

1. 展开相对路径和 `~`。
2. 规范化 `.`、`..`、分隔符和平台大小写。
3. 同时检查原路径、realpath 和父目录中的 symlink 表示。
4. deny 规则优先。
5. 对写入执行敏感路径检查，例如 `.claude` 设置、shell 配置和内部控制文件。
6. 判断是否位于 cwd 或 additional working directories。
7. 根据 read/write/create 与 mode 决定自动允许还是 ask。
8. 最后检查显式 allow 和 sandbox write allowlist。

删除操作另有根目录、home、根的直接子目录和通配符保护。

实现集中在：

- `claude-code/src/utils/permissions/{filesystem,pathValidation}.ts`
- `claude-code/src/utils/fsOperations.ts`
- 各文件工具的 `checkPermissions()`

## 8. Bash 权限

Bash 权限是多阶段分析，而不是黑名单正则：

- 解析 shell AST 和 compound command；
- 对每个子命令匹配 deny/ask/allow 规则；
- 检查 wrapper、环境变量、redirect、heredoc 和 command substitution；
- 检查 mode、只读语义、目标路径、危险删除和 sed 写入；
- 无法证明安全时回到 ask；
- 复杂命令拆分数有上限，超过上限按 ask 处理；当前快照为 50。

规则建议倾向 exact 或稳定的 `command + subcommand` prefix，并禁止自动建议 `bash:*`、`sudo:*` 等等价于任意执行的宽泛规则。

主要实现位于 `claude-code/src/tools/BashTool/bashPermissions.ts` 及同目录的 mode/path/readOnly/sed validation。

## 9. Permission 与 Sandbox

两者解决不同问题：

- **Permission**：基于用户意图、规则和工具输入决定是否允许。
- **Sandbox**：在进程或文件系统层限制已经允许的命令能影响的范围。

源码会让两者交换事实：已配置的 sandbox write allowlist 可以成为路径允许依据；确定会在 sandbox 中运行的 Bash 也可以跳过某些 ask。但 sandbox 不能替代规则优先级，permission 也不能提供 OS 级隔离。

## 10. 非交互场景

后台 Agent 或 headless 会话可能无法展示权限 UI。此时：

1. 先给 PermissionRequest hooks 一次明确 allow/deny 的机会。
2. 没有决定时自动 deny，而不是假定用户同意。
3. `dontAsk` 也把 ask 转成带原因的 deny。

每次决定都保留 `decisionReason`，便于 UI、SDK、审计和遥测区分“规则拒绝”“用户拒绝”“mode 拒绝”与“安全检查拒绝”。

## 11. 核心不变量

1. deny 与显式 ask 的优先级高于 allow、mode 和 hook allow。
2. 所有输入修改都必须发生在执行之前，并进入后续安全检查。
3. 无交互能力时 ask 必须 fail closed。
4. 路径检查必须覆盖 symlink 后的真实目标。
5. shell 解析失败意味着无法证明安全，不意味着安全。
6. Permission 和 Sandbox 必须是两个可独立测试的边界。

## 12. nano-code 的小型映射

Python 版本采用同样的两阶段结果：工具先返回可含 `passthrough` 的
`ToolPermissionResult`，`PermissionPolicy` 再收敛为最终三态
`PermissionDecision`。职责分配如下：

```text
Tool                 具体输入是否只读、工具特有规则和安全语义
PermissionPolicy     blanket 规则、mode、优先级和默认 ask
ToolExecutor         校验、请求确认、执行获批 input、生成 tool_result
PermissionPrompter   交互形式；不参与安全判断
PermissionUpdate     会话 context 或 settings 的结构化变更
```

Bash 分析器只自动允许能够静态证明安全的命令。简单管道和 compound command 会
逐段检查，所有子命令都通过才视为只读；prefix/wildcard allow 也必须覆盖每个
子命令，不能用 `Bash(git:*)` 放行 `git status && rm file`。规则字符串由
`permissions/rules.py` 解析并规范化：`Tool(*)`/`Tool()` 等价于整工具规则，
括号与反斜杠可转义，`*` 是 shell 通配符而 `\*` 匹配字面星号。

nano-code 使用 `tree-sitter` 与 `tree-sitter-bash` 解析 Bash，不再使用词法拆分
作为回退。首版只信任静态 simple command、引号拼接、`&&`/`||`/`;`/换行、
pipeline、静态环境前缀和静态文件重定向。变量、glob、substitution、后台任务、
subshell、控制流、函数与 heredoc 等复杂结构默认 ask；语法错误、未知节点和分析
预算超限同样 fail closed。复杂 AST 中能够可靠定位的内部命令仅参与 deny/ask，
不能支持自动 allow。AST 是权限判断输入，不是 OS sandbox。

静态输出重定向不会自动继承只读语义。prefix/wildcard 规则只有在全部子命令均被
覆盖且重定向 realpath 位于 cwd 内时才可批准写入；cwd 外目标、symlink 逃逸、动态
目标和改变 cwd 的 compound redirect 仍需完整精确规则或交互确认。输入重定向只有
cwd 内目标可自动只读，`/dev/null` 与文件描述符合并按安全特殊情况处理。只有
`LANG`、`LC_*`、`TZ`、`NO_COLOR` 等静态环境变量可继承底层命令规则，`PATH`、
`BASH_ENV` 和语言/工具行为变量不能继承普通 prefix 授权。

所有内置工具都显式实现输入级权限。Read/Glob/Grep 只自动允许 cwd 内且通过
realpath/symlink 边界检查的读取；Write/Edit 解释路径 content rule，并在
`acceptEdits` 中自动允许普通工作区编辑；Todo 只修改 transcript 投影状态。
`.git/` 与 `.nano-code/` 写入属于 bypass-immune safety ask，cwd 逃逸始终 deny。

持久规则写在 settings 的 `permissions.allow/deny/ask` 数组中，按
user < project < local 合并去重，重复规则保留 local > project > user 的
最高优先级来源。解析器只校验通用 `Tool` / `Tool(content)` 语法；content 由目标
工具解释。未知或尚未注册的工具规则会原样保留为 inactive，未来注册对应工具后
自然生效。

交互确认新增“Yes, and don't ask again”：Bash 会让用户输入命令前缀（如
`git diff:*`），Write/Edit 使用工具建议的精确路径规则；更新默认写入 gitignored
的 local settings，并立即更新当前权限 context。没有安全长期建议时不展示该选项，
safety ask 只能当次批准。显式 deny/ask 仍然优先于 allow。每次权限决策都会
通过 `nano_code.permissions` logger 记录：allow 为 info，deny 与被拒的 ask
为 warning，日志包含工具、behavior、message、结构化 reason 和是否提供反馈，
但不记录反馈原文，也不进入会话
transcript。

## 13. 主要源码入口

- `claude-code/src/types/permissions.ts`
- `claude-code/src/utils/permissions/permissions.ts`
- `claude-code/src/utils/permissions/{PermissionUpdate,permissionsLoader,permissionRuleParser}.ts`
- `claude-code/src/hooks/useCanUseTool.tsx`
- `claude-code/src/services/tools/toolHooks.ts`
- `claude-code/src/tools/BashTool/bashPermissions.ts`
