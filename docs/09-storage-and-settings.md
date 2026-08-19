# 存储与配置

## 目录边界

nano-code 采用 Claude Code 的分离方式：运行状态归入用户目录，仓库内只放项目配置。默认布局如下：

```text
~/.nano-code/
├── settings.json
├── providers.json        # Provider URL、协议和默认模型，不含密钥
├── .credentials.json     # 按 Provider 隔离的 API Key，0600
└── projects/
    └── <canonical-cwd-sanitized>/
        ├── <session-uuid>.jsonl
        └── <session-uuid>/
            └── tool-results/

<project>/.nano-code/
├── settings.json          # 可共享、可提交
└── settings.local.json    # 仅本机，已被 Git 忽略
```

项目目录名由规范化后的绝对路径生成：非 ASCII 字母或数字替换为 `-`；超长名称保留 200 字符前缀并追加稳定哈希。`NANO_CODE_CONFIG_DIR` 可替换整个 `~/.nano-code` 根目录。

路径解析和读取配置没有写副作用。Transcript 在第一条消息持久化时创建；`tool-results/` 仅在结果超过内联上限时创建。因此，仅运行 `nanocode --help` 或解析一次配置不会生成空目录。

真正启动聊天或认证命令时会幂等初始化用户目录：创建缺失的 `settings.json`、`providers.json`、空 `.credentials.json` 和 `projects/`，但不会创建仓库内的 `.nano-code/`。已有文件只校验、不覆盖；损坏的 JSON 会导致启动失败。旧 settings 或 Transcript schema 不会迁移、覆盖或删除，读取时会报告文件路径和重建提示。

## Provider Profiles

Provider Profile 是一个命名连接配置。支持 `anthropic-messages` 与官方 `openai-responses`；不同 Profile 可以分别设置 Base URL、模型、reasoning 和 API Key。非敏感定义保存在 `providers.json`：

```json
{
  "version": 3,
  "providers": {
    "anthropic": {
      "protocol": "anthropic-messages",
      "defaultModel": "claude-sonnet-4-6",
      "reasoning": {"enabled": true, "effort": "auto", "context": "auto"},
      "limits": {
        "contextWindowTokens": null,
        "maxInputTokens": null,
        "maxOutputTokens": null
      },
      "compact": {"triggerInputTokens": null}
    }
  }
}
```

`settings.json` 的 `activeProvider` 指向当前 Profile；API Key 只存在 `.credentials.json`，并以 `{"kind":"apiKey","apiKey":"..."}` 的可扩展鉴别联合保存。项目级配置不能定义或选择 Provider，避免不可信仓库重定向认证请求。Provider ID 在三类文件中使用同一校验规则。

旧 profile schema v2 可读，并解释为“不主动请求 reasoning、无 limits override、compact
使用 Auto”；下一次保存写为 v3。`triggerInputTokens` 是下一次请求预计 input token 的
绝对阈值；空值表示有效输入上限的 90%。能力未知时默认上限为 200,000 token，
对应默认 compact 阈值 180,000 token。保存时拒绝非正数及超过已知 profile 上限的值。
环境变量优先级为 `NANO_CODE_API_KEY`，随后按协议选择 `ANTHROPIC_API_KEY` 或
`OPENAI_API_KEY`；model/base URL 同样使用对应协议前缀。

模型目录缓存保存在私有 `.model-catalog.json`，按 profile、protocol 与规范化 endpoint
隔离且不含凭据。启动刷新超时为 3 秒，手工刷新为 10 秒；失败使用缓存、内置官方目录或
200,000-token fallback，并保留可观察错误。OpenAI `/v1/models` 只证明模型可用，不提供
context window；只有官方精确 model ID 才合并内置能力，第三方兼容模型保持未知并允许覆盖。

## 配置层与优先级

由低到高依次为：

```text
内置默认值 < 用户 settings.json < 项目 settings.json
             < 项目 settings.local.json < 环境变量 < CLI 参数
```

若从用户主目录启动，默认用户目录 `~/.nano-code/` 会与项目目录
`<cwd>/.nano-code/` 重合。此时 project/local 两层配置会被停用，避免同一个目录
同时承担用户配置和仓库配置作用域；用户配置和会话存储仍正常工作。

用户级 `settings.json` 保存当前 Profile 和通用行为：

```json
{
  "version": 3,
  "activeProvider": "anthropic",
  "agent": {
    "maxSteps": 100,
    "maxOutputTokens": 8192,
    "contextChars": 160000
  },
  "permissions": {
    "defaultMode": "default",
    "allow": ["Bash(git status)", "Read"],
    "deny": ["Bash(rm:*)"],
    "ask": ["Bash(git push)"]
  }
}
```

`permissions.allow/deny/ask` 中的每条规则按 `ToolName` 或 `ToolName(content)`
解析；`Tool(*)`/`Tool()` 等价于整工具规则。规则跨 user/project/local 合并
去重，重复规则保留 local > project > user 的最高优先级来源。规则解析不要求
工具当前已注册；未知工具规则保留为 inactive。Bash 解释命令 content，文件工具
解释规范化的 cwd 相对路径 exact/wildcard content。

权限确认产生结构化 `PermissionUpdate`。长期允许默认使用 local scope：settings
先原子写入成功，再更新当前内存 context；持久化失败时工具不会执行，也不会留下
只在内存生效的授权。

`maxSteps` 是可选的显式安全阀；省略时主 Agent 不设 Step 上限。它统计一次
Turn 内成功产生 AssistantMessage 的 Step，overflow/fallback 恢复中失败的 ModelCall
重试不额外计数。CLI 对应 `--max-steps`。settings version 3 不接受旧
`maxTurns`。Provider profile 和 credential 是独立 schema，仍使用 version 2。

## Config 与启动边界

`nano_code.config` 是路径、分层 settings、provider profile 配置与存储的唯一归属。`SettingsLayer` 表示单个持久化来源，`SettingsResolver` 统一合并文件、环境、profile、凭据和入口 overrides，最终生成运行时使用的 `AgentSettings` 快照。

根 `nano_code.bootstrap` 是唯一 composition root 和进程入口，负责创建 Agent、Chat、Context、Provider、Session 与 Tool 对象图。CLI 参数解析只产生 `SettingsOverrides`，不读取磁盘或凭据；TUI 只依赖 `chat` 的公开 API。`ChatService` 负责前端事件投影、活动 Session bundle、provider 切换与权限确认，不存在 Agent inbound port 或 CLI runtime adapter。

Provider 的默认模型和 endpoint 归入 `providers.json`；项目配置仍可用 `agent.model` 覆盖模型，但不能定义或选择 Provider。settings 不再接受 `baseUrl` 作为连接配置入口。

`contextChars` 只作为旧配置兼容和异常内存增长硬保护，不写入新 Provider 配置界面。
正常 compact 由 Profile 的 input-token 阈值驱动，并为输出 token 单独预留空间。

未知字段读取时忽略、写回时保留，已知字段类型错误则启动失败。`NANO_CODE_PROVIDER`/`--provider` 覆盖活动 Profile，`ANTHROPIC_BASE_URL`/`--base-url` 覆盖其 URL，`NANO_CODE_API_KEY` 或兼容的 `ANTHROPIC_API_KEY` 覆盖该 Profile 的持久化 Key。项目级和 local settings 均不能指定 Provider URL 或活动 Provider；共享项目配置同样禁止设置 `bypassPermissions`。

当前阶段不会自动清理 Session。Transcript 和工具结果会持续保留，直到未来明确引入手动管理能力。

配置和凭据写入接口使用同目录临时文件加原子替换；凭据目录为 `0700`，文件为 `0600`。持久化 Key 只传给 Provider，Bash 子进程会剥离 API 认证变量。会话 ID 必须是标准 UUID，避免路径穿越并保持恢复接口稳定。

凭据生命周期由 `nanocode auth login/status/logout` 管理，命令默认作用于活动 Profile，也可通过 `nanocode --provider <id> auth ...` 指定。当前故意不实现 OAuth 和跨平台系统密钥环；未来可在 `CredentialStore` 后增加 Keychain、Secret Service 或 Credential Manager 适配器，而不改变 CLI 和 runtime。

## 与参考实现的差异

长项目路径的哈希后缀使用 SHA-256，而参考快照在 Bun 环境使用 `Bun.hash`；二者目录形状相同，但极端长路径不会得到相同后缀。MVP 暂不实现 managed settings、CLI `--settings` 文件、会话索引、`state.json`、OAuth、Keychain、并行工具、Session fork 和旧原型目录迁移。
