# Provider、配置与存储

本专题说明运行配置从哪里来、哪些内容可以共享，以及 Provider 和 Session 文件各自承担什么职责。对话事务语义见[状态、对话与 Session](02-state-conversation-sessions.md)。

## 本地目录

默认用户目录为 `~/.my-code`，可用 `MY_CODE_CONFIG_DIR` 覆盖。主要路径由 `config.paths.MyCodePaths` 统一解析：

```text
~/.my-code/
├── settings.json              # 用户设置和 activeProvider
├── providers.json             # 非敏感 Provider profiles / 模型目录
├── .credentials.json          # 私有 API keys
└── projects/<normalized-cwd>/ # Session transcript

<workspace>/.my-code/
├── settings.json              # 可共享项目设置
└── settings.local.json        # 当前用户对项目的覆盖与信任

<system-temp>/my-code-<uid>/<normalized-cwd>/
├── <session>/tool-results/    # 外置大型工具结果
└── <session>/tasks/           # 后台任务输出
```

用户配置、项目状态和 runtime 临时文件分开存放。临时目录不是 durable Session storage；系统清理后不保证能恢复外置结果全文。

## Settings 分层

`SettingsStore` 以 `user -> project -> local` 顺序 overlay，后层只覆盖显式出现的字段。JSON 使用严格已知字段和 schema version；未知字段、错误类型或不支持版本会明确失败。

- User：跨项目默认值，可设置 active Provider、权限、工具与扩展。
- Project：可提交的仓库配置，但不能声明 Full Access、切换 active Provider，或直接信任并启动 MCP server。
- Local：当前用户对该项目的私有覆盖；用于敏感的信任决定。

命令隔离使用 v3 的 `sandbox` 段：`mode` 为 `auto | local`，`network` 为 `restricted | enabled`，`allowUnsandboxedCommands` 控制模型能否逐次申请宿主执行，默认分别为 `auto`、`restricted` 和 `true`。只有 User 与 Local 可以配置；共享 Project settings 包含该段时加载失败，防止仓库关闭隔离、开启网络或授权越界。运行时保存启动时的实际 backend 和 fallback 原因，不把配置意图误报为已启用 sandbox。

Provider profile 和 API key 不进入普通分层 settings。运行时读取完整、不可变的 `AgentSettings` 快照；更新操作先原子写文件，再替换对应 runtime 状态。

## Provider profiles 与模型目录

`providers.json` 保存具名 profile：协议、Base URL、默认模型、模型目录、reasoning 偏好、模型限制和 compact 设置。当前支持 Anthropic Messages 与 OpenAI Responses；模型边界本身由 `model.ModelClient` 保持 provider-neutral。

首次启动的 Provider 向导或 `/provider` 使用模型目录 API 检查 Endpoint 和认证，不发送生成请求。成功探查后把去除凭据与 SDK 对象的目录保存进 profile；启动和 `/model` 只读取本地目录，不隐式联网。手工模型会标为未验证，已知不兼容模型不能被选择。

`/model` 在 operation lock 内持久化当前 profile 的默认模型并更新 `ProviderRuntime` 的 model environment。Provider switch 原子切换前台 router、环境和新 lease 的连接；已存在 child lease 继续使用创建时的 connection。

## 凭据

`auth.credentials.CredentialStore` 只管理私有 `.credentials.json`，每个 Provider 只有 `stored` 或 `none` 的可观察来源。API key 不进入 settings、profile、Session、状态投影、日志或诊断。

Provider ID、模型、URL 和 key 不从 `ANTHROPIC_*`、`OPENAI_*` 或旧 Provider 环境变量隐式解析；SDK client 总是接收显式解析后的连接数据。MCP 的 `envFrom` 只保存目标变量名到来源变量名的映射，不保存环境值。

## Session 与临时结果

Session transcript 位于 project state 目录，由 `sessions` 私有 record/codec/store 维护。写入使用同目录临时文件和原子替换；恢复严格校验 schema、父链、tool pairing、compact boundary 和 replay 关联。

大型工具结果和后台 Bash 输出位于 project-scoped runtime temp root，目录与文件使用私有权限。Transcript 保存有界 preview、presentation 和受控路径；调用方不能直接绑定或切换结果 store。

## 版本控制与安全

可以提交项目 `.my-code/settings.json`。不得提交：

- `.my-code/settings.local.json`；
- API key 或 `.credentials.json`；
- Session transcript、本地工具/任务输出；
- `.venv` 或 Provider 探查的敏感输出。

主要源码入口：`src/my_code/config/paths.py`、`src/my_code/config/store.py`、`src/my_code/config/providers.py`、`src/my_code/auth/credentials.py`、`src/my_code/providers/router.py`。
