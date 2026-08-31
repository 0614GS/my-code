# 终端 UI

`tui` 是 `mycode` 的交互式 host。它使用 `prompt_toolkit + Rich` 处理输入、底部动态区、临时面板和事件渲染，但不读取 JSONL、不执行工具、不构造 `ModelRequest`，也不持有 AgentEngine。

## Host 边界

主应用使用普通 terminal scrollback，而不是全屏 alternate buffer。已完成内容先在单 worker renderer 中离屏生成，再由唯一 UI owner 串行、短暂地写入 terminal；composer、队列预览、状态栏和临时交互保留在底部动态区。只有 `Ctrl+T` transcript pager 临时使用 alternate buffer。

TUI 通过 `ChatService` 执行用户级操作，并从各领域的公开语义模块读取安全 DTO。它不会直接访问 Session、Conversation、Context、ToolExecutor、ToolCatalog、TaskSupervisor、MCP/Skill runtime 或 Provider SDK。

`@path` 的解析、附件准备与路径建议由 Chat 的 mention 子模块拥有；TUI 只消费 Chat 提供的 frontend-neutral suggestion 与加载结果。

模块按 UI 职责拆分：`app.py` 负责编排，`turns.py` 消费事件，`layout.py` 和 `key_bindings.py` 管理动态区与按键，`picker.py`/`panels.py` 管理临时交互，`widgets.py`/`theme.py`/`terminal.py` 管理展示和终端能力。

## 事件与展示

一次 prompt 调用 `ChatService.stream()`，TUI 消费 text/reasoning started、delta、completed，显式 model-step completed、完整 compact started/completed、attachment、tool started/finished、Todo/context 更新和 turn 终态。

- delta 只存在于动态区；completed 到达后先清除预览，再把最终 Rich renderable 固化到 scrollback。
- Markdown live projection 缓存稳定块，只重画不稳定尾块；Rich 输入上限 16 KiB、动态区最多 12 个视觉行，超长未闭合块退化为 8 KiB plain-text tail。最终 completed 内容仍完整渲染。
- reasoning live view 保留原始换行并跟随尾部多行；终态只固化 Provider disclosure 允许显示的完整段落。
- 完整 display block 暂存到 model-step completed；含工具的连续 step 组成一个工作组，只有工作组转入无工具最终回答时才插入一条全宽暗色分隔线。纯对话回答不显示空分隔线，steering 用户输入建立新工作组。
- 连续 ToolCall 组成一个稳定有序块，并行完成只按 call ID 更新原位置，不重排。
- `Edit`/`Write` 在底部动态区只显示紧凑状态和结果摘要；成功块固化到 scrollback
  或从 Session 恢复时，展开持久化的内联 diff。权限确认、失败和取消路径不展开 diff。
- 异常、取消、max-steps 和后台 continuation 使用同样的“移除动态副本后固化终态”边界。
- 启动和 Session 恢复时状态栏只显示模型与 entry 数，不计算或展示推测性的 context usage。首条用户输入 canonical commit 后生成第一次 context snapshot；此后在每个已提交的工具 step、turn 结束和已显式展示 context 后的配置变化等安全边界刷新。`/status`、`/context`、`/usage` 仍可按用户明确请求即时测量。重绘 callback 不重新执行 context 规划，工具 step 的 snapshot 只在 assistant tool call 与对应 tool results 均提交后生成。
- composer 上方的活动行由独立的 TUI state/controller 和独立单行 Window 管理，固定排列在 thinking、工具和流式模型内容之后。它使用单调时钟显示 spinner、阶段标题和整次 invocation 耗时；阶段切换只更新标题，不重置时钟。唯一 ticker 只触发廉价 `FormattedText` redraw，不进入 Rich/Markdown renderer，关闭时随 UI tasks 回收。
- `/compact` 使用独立操作时钟；auto/reactive compact 保留当前 invocation 时钟。成功事件更新 context snapshot，并把触发来源与压缩后 usage 写入 scrollback；失败或取消只走既有终态错误路径。microcompact 保持静默，压缩进度不写 canonical Session。

恢复历史和实时事件使用相同的安全投影。每次成功 TodoWrite 都携带稳定 call ID 和完整写入快照进入 frontend-neutral 事件，即使清单内容相同或已经全部 completed；“当前活跃 todo 状态”仍可在全部完成后为空。工具展示优先使用执行时持久化的 presentation；dispatcher 目标由 Chat/feature 投影解包，TUI 不重新解释原始 Conversation 或读取外置结果文件。

内联 diff 使用固定旧/新行号 gutter、增删整行背景、按扩展名选择的语法着色，并对
相邻的小范围增删做保留空白的词级加深。长行按终端宽度折行且续行不重复行号；Tab
按 4 列展开，控制字符转为可见文本。分离 hunk 和被裁剪区显示省略标记，最多消费
presentation 中的 200 条代码行记录。

## Transcript 与 Agent 视图

`Ctrl+T` 打开当前 Session 的三级 audit transcript。timeline 包含用户/助手文本、允许披露的 reasoning、工具参数与模型实际看到的持久化结果、summary、durable attachments，以及精确 model/compact request 边界。system prompt section 和 tool schema 首次出现时展开，未变化请求显示稳定引用；旧 Session 没有 sidecar 时显示明确缺口。

`n/N` 在请求间移动，Enter 打开当前 request 的 resolved view：完整 section、有序 input 及 origin、tool definitions/JSON Schema、输出预算、reasoning mode、ContextBudget 和终态。resolved view 中 Esc 返回 timeline；timeline 中 Esc、Ctrl+T 或 q 关闭。pager 的 Session/audit revision 在单 worker 中 latest-wins 渲染，UI callback 不解析 JSONL。

`/agents` 和 F6 可查看主会话、活跃 child 与最近结束 child 的进程内安全视图。选择只改变 UI 投影，不切换核心 Session，也不取消 child。

## 输入、命令与临时面板

Enter 提交，Shift+Enter 或 Ctrl+J 换行；Esc 按候选菜单、临时面板、当前 invocation 的优先级处理。Agent 与后台 continuation 运行时 composer 仍可编辑，普通 Enter 加入仅内存的 steering queue；accepted 事件到达后才写入 scrollback。空 composer 按 Up 可取消准备任务并召回最近的 pending/failed 输入。Esc 只取消当前 invocation，不清除队列。

composer 下方最多显示三行 queue preview。Slash command 显式分为 `concurrent_read`、`concurrent_ui` 与 `exclusive`：只读命令、纯展示偏好和 permission mode 可在 Agent active 时运行；permission mode 切换从下一 step 生效。compact、能力 reload/reconnect、provider/model、resume 和 exit 等独占操作保留原草稿并等待 Agent 空闲。

composer 底栏显示模型、context entry 数量、按需计算的 context usage 和警告；Plan 时底栏右下角显示 `Plan` 指示器。普通 composer 在空闲、空输入且无独占面板时用 Shift+Tab 切换 Default/Plan，provider form 中 Shift+Tab 仍表示上一字段。Plan 中 `/permissions` 与权限 Picker 禁用。

Question 使用独占面板逐题展示 2–3 个单选项，并由 TUI 自动增加 Other 文本输入；Escape、turn 取消、Session 切换和关闭都会解除等待并让工具结果闭合。成功 turn 收到非空 `<proposed_plan>` 后显示三项交接：当前上下文实施、全新上下文实施、留在 Plan。当前上下文会在下一次提交追加 Default mode、Question invalidation 和实施消息；全新上下文只携带 durable plan handoff 与实施消息；留在 Plan 不产生模型可见事实。

`/view` 使用与 `/model`、`/provider`、`/resume` 相同的二级 Picker 选择主界面模式；`/view concise` 与 `/view detailed` 仍可直接原子写入用户级 `tui.viewMode`。默认 concise，偏好跨项目和 Session。切换会清屏并按新模式重新投影当前 Session，复用 resume 的历史渲染路径。Detailed 在每次请求前按 audit ID 去重展示新增/变化的 AGENTS、Attachment、Todo/Skill、工具发现与后台通知正文；恢复历史时从 request audit 投影相同内容。单项限制为 60 行和 8 KiB，并完整格式化 canonical ToolCall input。工具结果继续使用安全摘要/diff。Concise 保持原有展示；完整历史使用 Ctrl+T。

Slash commands 在进入模型前本地解析。已执行命令先在 scrollback 回显；`/status`、`/context`、`/usage`、`/tools`、`/skills`、`/mcp` 和 `/tasks` 使用统一的圆角信息卡，命令与卡片作为一个串行输出批次提交。`/resume`、`/provider`、`/model`、`/permissions` 和 `/agents` 等仍只调用 ChatService 的窄用例接口。选择器共用稳定 action key、可视窗口、导航和草稿恢复语义。

Permission、Full Access、Provider 和 Resume 共用 composer 下方的 interaction host。工具权限默认安全拒绝；无 OS sandbox 时首次进入 Full Access 必须经过当前进程有效的危险确认。API key 输入使用密码处理，面板只显示是否已配置。

## 生命周期

首次启动缺少有效 Provider 时，先完成 Provider 向导再组装聊天 runtime。退出时 TUI 关闭 transcript、取消 UI watcher、清除底部动态布局并交还光标；外层随后按 AppState 顺序关闭任务、扩展和 Provider。

主要源码入口：`src/my_code/tui/app.py`、`src/my_code/tui/turns.py`、`src/my_code/tui/layout.py`、`src/my_code/tui/key_bindings.py`、`src/my_code/chat/service.py`。
