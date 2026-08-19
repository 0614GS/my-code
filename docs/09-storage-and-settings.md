# 存储与配置

## 所有权

`config` 拥有路径、分层 settings、provider profile 和配置持久化。`auth` 拥有 API key 存储与解析。`sessions` 拥有 Conversation JSONL，`tools.result_store` 拥有大型工具输出文件。

根 `my_code.bootstrap` 是唯一 composition root，负责初始化存储并组装运行时对象；其他模块不能导入 bootstrap。

## 本地目录

```text
~/.my-code/
├── settings.json
├── providers.json
├── .credentials.json
├── model-cache/
└── projects/<workspace-key>/
    ├── sessions/<session-id>.jsonl
    └── tool-results/<session-id>/...

<workspace>/.my-code/
├── settings.json
└── settings.local.json
```

用户目录在真实启动或认证操作时幂等初始化。导入模块不会创建文件、读取凭据或发起网络请求。

## Settings 合并

`SettingsStore` 按 user、project、local 顺序叠加持久配置；入口 `SettingsOverrides` 再覆盖允许的运行参数。`SettingsResolver` 结合活动 provider profile、环境变量、凭据和模型能力，生成不可变 `AgentSettings` 快照。

Provider endpoint、protocol、默认 model、reasoning、limits 和 compact 配置属于 `providers.json`。项目 settings 可以覆盖 Agent model 等运行参数，但不保存 API key。

## 凭据

`auth.credentials.CredentialStore` 只处理私有凭据文件。解析顺序由 provider/protocol 和环境变量决定，并返回 `ResolvedCredential` 说明来源。任何 status、history、日志或 Session record 都不能包含 API key。

## 写入语义

- settings、profiles 和凭据使用临时文件加原子替换。
- PermissionUpdate 先写目标配置，再修改活动 PermissionPolicy。
- Session 只追加 JSONL；恢复时严格校验 schema 和父链。
- ToolResultStore 按 session ID 分目录，Chat resume 时创建新的 store。

## 版本控制

可以共享项目 `.my-code/settings.json`。不得提交 `settings.local.json`、凭据、Session JSONL、工具输出、`.venv` 或 API key。
