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

真正启动聊天或认证命令时会幂等初始化用户目录：创建缺失的 `settings.json`、`providers.json`、空 `.credentials.json` 和 `projects/`，但不会创建仓库内的 `.nano-code/`。已有文件只校验、不覆盖；损坏的 JSON 会导致启动失败。旧版顶层 `baseUrl` 和 `anthropicApiKey` 会复制或迁移到默认 `anthropic` Provider。

## Provider Profiles

Provider Profile 是一个命名连接配置，而不是另一套 SDK。当前唯一协议为 `anthropic-messages`；不同 Profile 可以分别设置 Base URL、模型和 API Key，最终都由 Anthropic Python SDK 发起请求。非敏感定义保存在 `providers.json`：

```json
{
  "version": 1,
  "providers": {
    "anthropic": {
      "protocol": "anthropic-messages",
      "model": "claude-sonnet-4-6"
    }
  }
}
```

`settings.json` 的 `activeProvider` 指向当前 Profile；API Key 只存在 `.credentials.json`。项目级配置不能定义或选择 Provider，避免不可信仓库重定向认证请求。

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
  "version": 1,
  "activeProvider": "anthropic",
  "permissions": { "defaultMode": "default" },
  "maxOutputTokens": 8192,
  "contextChars": 160000
}
```

`maxTurns` 是可选的显式安全阀；省略时主 Agent 不设模型轮数上限。它统计的是一次
用户输入内的模型 API 往返，而不是用户消息数量。

## Core 与启动边界

`nano_code.core` 是 settings 与应用启动的唯一归属。`SettingsLayer` 表示单个持久化
来源，`SettingsResolver` 统一合并文件、环境、provider、凭据和入口 overrides，最终
生成一次 Agent 生命周期使用的 `AgentSettings` 快照。`core.bootstrap` 是唯一完整
composition root，负责创建 Agent、context、provider、session 和 tool 适配器。

CLI 参数解析只产生 `SettingsOverrides`，不读取磁盘或凭据；`cli.runtime` 只把
`AgentInboundPort` 的事件和状态投影为 TUI contract。这样未来增加非 CLI 入口时，
不需要复制 settings 优先级或 Agent 依赖装配。

Provider 的模型和 Base URL 归入 `providers.json`；项目配置仍可用 `model` 覆盖模型，但不能定义或选择 Provider。旧版用户设置中的顶层 `model`、`baseUrl` 只作为首次创建 Provider Catalog 时的迁移来源。

`contextChars` 是本地工作集触发 compact 的保守字符阈值，不代表 Provider 宣称的
精确 token window。实际预算会优先使用最近一次模型响应的 usage，并为输出 token
单独预留空间。

未知字段会被忽略，已知字段类型错误则启动失败。`NANO_CODE_PROVIDER`/`--provider` 覆盖活动 Profile，`ANTHROPIC_BASE_URL`/`--base-url` 覆盖其 URL，`NANO_CODE_API_KEY` 或兼容的 `ANTHROPIC_API_KEY` 覆盖该 Profile 的持久化 Key。项目级和 local settings 均不能指定 Provider URL：MVP 尚无 workspace trust，仓库控制的地址可能接收用户的 API Key。共享项目配置同样禁止设置 `bypassPermissions`。

配置和凭据写入接口使用同目录临时文件加原子替换；凭据目录为 `0700`，文件为 `0600`。持久化 Key 只传给 Provider，Bash 子进程会剥离 API 认证变量。会话 ID 必须是标准 UUID，避免路径穿越并保持恢复接口稳定。

凭据生命周期由 `nanocode auth login/status/logout` 管理，命令默认作用于活动 Profile，也可通过 `nanocode --provider <id> auth ...` 指定。当前故意不实现 OAuth 和跨平台系统密钥环；未来可在 `CredentialStore` 后增加 Keychain、Secret Service 或 Credential Manager 适配器，而不改变 CLI 和 runtime。

## 与参考实现的差异

长项目路径的哈希后缀使用 SHA-256，而参考快照在 Bun 环境使用 `Bun.hash`；二者目录形状相同，但极端长路径不会得到相同后缀。MVP 暂不实现 managed settings、CLI `--settings` 文件、会话索引和旧原型目录迁移。
