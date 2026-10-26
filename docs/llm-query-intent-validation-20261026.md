# 查询意图改造与真实图纸验证（2026-10-26）

## 交付范围

实现协议见 [LLM 问题理解、精确执行与证据校验](llm-query-intent.md)。默认由模型先理解请求并登记结构化意图，再选择本地工具；本地不再依据“地下室”“做法”等关键词抢先执行整类检索。对象、编号、范围、否定条件分别保留，额外名称检索需要模型显式给出原因。交付校验编号、范围引用、完整读取与多目标覆盖；裁剪后的整理上下文不会重新注入全部历史正文。

在 `feat/llm-query-intent-20261026` 开发，通过集成分支合入复杂表格主分支 `acf97ab` 与文本锚定主分支 `83236d1`。集成产品提交为 `e9b8298`，同时保留新模式的模型先行、失败调用回滚和旧模式兼容开关。主工作目录中的并行未提交工作未被覆盖。

## 真实模型验证

真实快照：`snap_db5b8dafb2267ec84e834491`。所有运行通过产品 `Workspace.start_chat` 入口，冻结产品代码与真实图纸数据；使用现有 `responses_subscription` 订阅，`allow_fallback=false`、`source_read_batching=false`。用户明确授权此次图纸片段发送至现有订阅的 Responses 目的地。证据目录均位于集成工作树的 `artifacts/`，未将图纸或账号凭证提交 Git。

| 场景 | 运行与证据 | 结果 |
| --- | --- | --- |
| 地下室内墙4，最终主分支集成版本 `e9b8298` | `real-merged-main/`；`chat_51b3269bedeb4919b444c7647658f129` | completed；审计通过；1 个候选、14 条精确引用；六层正文及备注保留 |
| 地下室内墙4，合并复杂表格后的版本 `30900d7` | `real-latest-main/`；`chat_9dca70b7b64a4804aca012e0426d6646` | completed；审计通过；1 个候选、13 条精确原文引用；六层正文及备注保留 |
| 排除地下室的内墙4 | `real-exclusion-fixed/`；`chat_331c3bca7b9e46b4ac56a3ac4dbcbcd2` | completed；审计通过；3 条引用；未把含地下室范围的候选作为满足排除条件的结果 |
| 内墙4与内墙5及失败任务恢复 | `real-multiple/resume-task/`；`chat_21516a5ef10a46a29703209bd338a039` | completed；审计通过；2 个目标分别回答、21 条引用；恢复前后意图 ID 不变 |

这些任务均为 `partial`：读取与引用可用，但表头/字段归属尚未完整确认。系统明确呈现候选原文和证据缺口，没有将其提升为工程实例适用性结论。排除场景也不据此断言整个项目不存在其他符合条件的做法。

多目标最初运行因上游订阅事件流 `SIWC_RESPONSE_FAILED` 失败，随后恢复已有任务完成。早期共享账号排队超时未发送模型请求；这些记录均保留，不计为功能通过。一次排除场景揭示模型传入 `subject=内墙4`、`identifier=4` 时重复拼接编号的缺陷，已修复并由独立真实运行复验。完整别名编号另有最长前缀回归；`real-final-local/` 的原图读取结果与 `real-latest-main/` 的 `read_assembly` 观察逐字段相同，见 `final-alias-replay-comparison.json`。

最终模型运行中，模型尝试按图框查看表格时触发已有兼容问题：恢复表格的单元格缺少旧接口要求的 `region_ids`。模型随后读取局部图像并完成带缺口的回答；没有将这次工具错误隐去。最终补充单元格引用对应的真实 region ID，不引入范围猜测；同一图纸、同一失败参数的本地重放成功返回 4 张表，逐一核对图框来源，见 `artifacts/table-frame-real-replay.json`。这项最后修复经针对性测试和真实图纸工具重放验证，真实模型冻结版本仍为上表所列 `e9b8298`。

## 回归与证据边界

- 开发分支完整 Python 套件：1834 passed、56 skipped、1 failed（旧测试要求对有裁剪标记的检查点重新注入全部正文）。该断言已按新上下文契约拆分修正，相关 16 项通过；未将这次全套运行宣称为零失败。
- 集成订阅运行时、上下文与查询协议回归：166 passed。
- 合并复杂表格后的编号、表格、来源与查询兼容回归：196 passed、1 skipped。
- 完整别名编号最终回归：43 passed。
- 最后合入主分支 `83236d1` 后，表格、编号、查询、来源、工具注册和读取上下文回归：234 passed、1 skipped；日志 `artifacts/merged-main-final-tests.log`。
- 表格来源字段修复后的语义表格、通用工具、真实表格形态与查询回归：83 passed；日志 `artifacts/table-frame-compatibility.log`。
- 前端完整 Node 套件：59 passed。

原始日志分别为开发树的 `artifacts/query-intent-full-tests-vfinal.log`，以及集成树的 `artifacts/integration-targeted-v2.log`、`read-context-corrected.log`、`latest-main-compatibility-final.log`、`query-intent-final.log`、`latest-main-node.log`。本地回放、单元测试、真实模型运行和原文审计分别记录，不能相互替代。

## 复验方式

使用 `scripts/validate_query_intent_real.py`，代码变化必须选择新的输出目录。每个目录保存 `code-manifest.json`、`source.json`、`request.json`、`run.json`、`task.json` 和运行章节；`scripts/audit_query_intent_validation.py` 对保存结果核对模型先行、禁止隐式扩展、引用与真实原文片段的一致性。`--offline` 仅表示确定性本地回放。主分支合并不包含在线服务重启。
