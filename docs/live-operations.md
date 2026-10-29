# 运行看板与任务热控制

任务中心的 `/operations.html` 展示事实任务和 E0 探索运行；任务中心及规则页都有入口。页面每 3 秒读取中心状态，Worker 每轮回读 Agent 状态并上报；这不是逐 token 流。后台标签页停止页面轮询，不停止服务器任务。旧版本任务没有采样记录时显示“未报告”。

## 可观察内容

- 实际执行阶段、任务状态、墙钟运行时长与暂停时长。
- 模型已报告的 token、模型/工具调用数量。缺失用量保持未知，重试按 attempt 分开记录。没有价格配置时不估算金额。
- 执行 Agent 进程的 CPU 与 RSS。进程可能服务多个任务，不能把这些值解释为任务独占消耗；macOS 回退采样会标明峰值内存。E0 子任务共享进程时不重复累加内存。
- 协调任务、规则 case、Worker、执行 Agent、模型请求和工具调用的节点及连线。节点基于实际日志或调度记录；人工/监督引导也有独立来源。此图不展示模型私有推理，也不把单个执行 Agent 描绘成多 Agent 团队。
- 控制请求、版本、实际执行回执、生效节点和错误。旧 attempt 的回执不能覆盖新 attempt；失联或过期遥测会标示。

## 控制语义

| 操作 | 生效方式 |
| --- | --- |
| 暂停排队任务 | 阻止 Worker 领取，保存后可重启恢复 |
| 暂停运行任务 | 等到模型/工具安全边界；当前在途调用允许结束 |
| 继续任务 | 同一运行继续推进；暂停时长不计入活动运行超时 |
| 人工引导 | 追加带来源的后续输入，不替换原始规则或图纸快照 |
| 运行预算 | 后续模型/工具边界读取新限制，不退还既有消耗 |
| 沙盒限制 | 后续容器启动使用新上限；不迁移正在运行的容器 |
| 附加校验 | 后续交付需满足配置的最少已验证证据数 |

暂停保留当前 Worker/Agent 执行槽和租约，不等于释放机器资源或保存可跨进程恢复的模型计算。进程重启遵循原有租约/任务恢复规则；不能声称中断的外部模型请求可原地恢复。取消在暂停状态仍有效。

热配置只作用于当前运行，并保留 before/after 审计。E0 的原始 scope/hash 保持不变，运行覆盖配置另行记录。E0 暂停首先关闭新 case 调度，再向已有任务发送暂停；看板明确显示执行端尚未确认的请求。

## 可调整字段

`configure` 的 payload 可以包含以下任意非空分组：

```json
{
  "budget": {"max_rounds": 30, "max_tool_calls": 80, "timeout_seconds": 900, "max_reported_tokens": 100000},
  "sandbox": {"timeout_seconds": 60, "memory_mb": 256, "max_output_bytes": 1048576},
  "validator": {"require_evidence": true, "min_evidence_count": 2}
}
```

运行轮次为 1–128，工具次数为 1–512，活动时间为 1–7200 秒，已报告 token 阈值为 1–1000 万。沙盒为 1–300 秒、64–512 MB、1024–2097152 字节；工具自身更短的剩余 deadline 仍优先。附加证据数为 0–20。

E0 还可设置 `dispatch_budget`：`max_case_starts`、`max_concurrent_cases`、`max_reported_tokens`，约束后续调度。运行 token 限制在用量回报后检查，不能阻断已发送请求的生成；未知用量不能被当作真实零用量。新增限制低于既有消耗时会阻止后续工作，不抹掉已有事实。

不能通过热配置增加文件挂载、网络访问、模型凭据或动态代码权限。原始明确禁止优先；引导文本和监督建议不能授权代码。基础来源真实性、引用和授权校验始终生效。当前支持 `native` 与 `responses_subscription` 的安全节点控制；不支持的运行时会拒绝并记录原因。

## 外部模型监督

默认关闭。可选择现有服务端 `api_sol` / `api_astra` 配置：

- `advisory`：只记录建议。
- `guidance`：把建议提交到执行端的后续输入队列，仍须等待安全节点确认。

监督模型只读取有界的运行摘要：阶段、使用量、控制状态、节点和交互状态。它没有工具执行能力，不接收原始图纸证据包或密钥，也不自动修改预算、校验器、沙盒及权限。监督模型凭据沿用现有 Agent 配置；中心只调用固定本机节点的认证接口。

监督间隔 30–3600 秒，最多 1–100 次，单独设置累计 token 预算。每次准入预留 24000 token 账面额度，已报告用量回填；失败、未知用量和中断请求保留预留，不自动免费重试。这是调度预算，不是模型供应商的硬 token 封顶。无活动执行任务或状态无变化时不会自动重复检查。监督建议期间人工修改控制、禁用监督、任务结束或 attempt 改变，会阻止旧建议自动介入。

## API 与审计

浏览器继续使用任务中心登录及同源保护。Worker 使用既有 Bearer 身份和 attempt 租约；Agent 控制请求还绑定 `X-Fleet-Execution`。监督来源由中心内部调用指定，浏览器不能冒充监督模型。

- `GET /api/fleet/operations`：总览。
- `GET /api/fleet/jobs/{id}/operations`：单任务图与遥测。
- `GET /api/fleet/exploration-runs/{id}/operations`：E0 聚合运行图。
- `POST /api/fleet/{jobs|exploration-runs}/{id}/control`：控制。
- `GET/POST /api/fleet/{jobs|exploration-runs}/{id}/supervision`：监督状态/配置。
- `POST /api/fleet/{jobs|exploration-runs}/{id}/supervision/review`：请求提前检查，仍受启用和预算约束。

控制请求格式为 `{command_id, expected_revision, action, payload}`，action 为 `pause/resume/guidance/configure`。guidance payload 为 `{message}`；pause/resume payload 为空对象。版本冲突返回 409，页面保留草稿并要求读取最新状态后重新提交；同一 command_id 绑定同一操作，重试不能重复执行。

监督配置请求为 `{command_id, expected_revision, config}`。config 包含 `enabled/model_profile/mode/interval_seconds/max_reviews/max_total_tokens/instructions`。执行端 HTTP 接受不等于配置生效；只有安全节点回执才标记 applied。

中心在原 Fleet 数据库保留控制、回执和按 attempt 的遥测，E0 控制保存在探索数据库，监督配置和结果保存在 `supervision.sqlite3`。现有全量备份覆盖这些文件。部署前需要所有任务空闲；暂停中的任务仍是活动任务，不应在发布时强行中断。

## 验证边界

代码回归与合成数据 UI 验收验证控制闭环和边界，不等于真实图纸质量验收。此功能交付不自动启动真实图纸或监督模型；真实模型协作质量、长时间探索资源走势及最终规则优化结果需单独验收。
