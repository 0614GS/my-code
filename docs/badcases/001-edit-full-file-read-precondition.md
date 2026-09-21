# Edit 全文件读取前置条件导致无效重试

## 现象

在 `jobs/mycode-swe-10` 的 10 个 SWE-bench trial 中，`Edit` 共调用 32 次，13 次失败，失败原因均为：

```text
ToolExecutionError: Read the entire current file before editing it
```

其中 11 次相同 Edit 随后重试成功；9 个 trial 遇到该问题，但其中 8 个最终仍通过。这类失败主要消耗步骤、token 和时间，不等同于任务解题失败。

## 根因

当前 `FileReadTracker` 只在同一 Session 对同一文件版本累计读取区间完整覆盖 `1..total_lines` 后，才允许 `Edit`。模型即使已经读取待修改区域，也会被拒绝。`Read` 单次输出还受 16,000 字符限制，因此 `limit=5000` 不保证读完整个文件，大文件需要反复跟随 `next_offset`。

Claude Code 参考实现只要求该文件至少成功执行过一次显式 `Read`，随后检查文件是否在 Read 后变化，并校验 `old_string` 存在且唯一；它不要求全文读取，也不要求读取区间覆盖修改位置。

## 判断与落地

当前规则把适合完整覆盖写入的约束同时施加给了精确局部替换，严格程度高于 Claude Code，且与大文件分页机制冲突。

规则按以下方式拆分：

- `Edit`：任意有效显式 Read 建立观察资格，继续使用 mycode 的完整 fingerprint、精确字符串校验和原子写入防止陈旧覆盖。
- `Write` 覆盖现有文件：继续要求完整 Read。
- Full compact 后，重新校验的截断文件快照恢复 Edit 资格，但不恢复 Write 资格。
- Analyzer 同时识别新旧前置条件错误文案，历史实验仍归入 `precondition_failed`。
