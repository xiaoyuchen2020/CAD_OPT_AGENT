# 按任务选择工具返回视图

> 2026-10-23 更新：v2 增加模型历史的确定性检查点，复用本文的领域数据包与证据档案；工具返回视图的授权语义不变。详见 [v2 上下文规则](cad-agent-execution-v2.md)。

`analysis/payload_projection.py` 在 `invoke_tool` 的正常执行路径中、观察和覆盖登记之前运行。它复用 `evidence_store` 的任务授权归档；没有新增数据库或独立文件存储。`artifact_storage` 用于共享 CAD 输入，不用于本轮工具记录。

工具调用的通用 envelope 接受 `result_view`：

| 值 | 行为 |
| --- | --- |
| `auto`（默认） | 仅高效策略启用；所属任务要求 `geometry` / `contours`、直接读取几何详情、或无法确定任务归属时保留完整返回。其他已列入白名单的几何工具可省略纯坐标数组。 |
| `summary` | 对白名单工具请求坐标摘要；所有候选行和非坐标字段保留。 |
| `full` | 保留当前工具返回，跳过大包摘录；仍受单次响应长度上限约束。 |

`result_view` 由 `invoke_tool` 提取，不传给底层具体参数模型；不改变 REST 几何 API。批量来源读取保持已有子项参数契约，不把 envelope 广播到子调用。缓存区分显式视图和所属任务导出的有效视图，避免摘要结果遮蔽随后请求的详情。

投影只识别 `boundary / holes / paths / vertices / coordinates / segments / internal_rings / geometry` 下由二维或三维有限数字坐标组成的数组。数组至少含 8 个点，且扣除投影元数据后至少省下 1024 个字符才应用。字典形式的几何状态、空数组、候选身份、实体与来源 ID、单位与冲突、范围、bbox、缺口、覆盖、分页和下一步调用均保留。原文证据和可执行请求不裁剪。

返回 `payload_projection.omitted_fields` 逐项说明省略的字段路径、出现次数、坐标点数及字符数；路径使用 JSON pointer 转义，列表位置用 `*`。`omitted_fields_read=false` 明确未读取状态，`candidate_rows_omitted=0` 说明没有借投影跳过候选。

原始工具返回通过 `evidence_record.record_id` 绑定本任务，可用现有 `read_evidence_record(record_id, start, length)` 分页读取。入口检查当前任务是否开放该记录、来源是否属于当前允许的快照、来源哈希及记录内容哈希。知道 ID 不能获得其他任务的读取资格。归档读取只返回数据，不增加测量、引用或覆盖资格。

观察与覆盖登记使用投影后的返回。候选行和其标量仍可登记；被省略的坐标不进入该次观察。内部几何服务、候选对象注册表和渲染高亮仍可保有计算结果，不等于模型读取或工程核验。已投影的数据包不会再经过通用大包摘录，否则会隐藏仍需登记的候选行。剩余内容仍超限时使用现有显式失败与回滚路径。

“完整”是完整的本次工具返回，不是完整原始 CAD。已有工具自身的分页、原生曲线细分和截断限制不变；例如 `read_geometry` 的原生完整记录仍通过现有 `full_geometry_record` 读取。

验证：

```sh
.venv/bin/python -m pytest tests/test_payload_projection.py -q
```

专项测试覆盖真实 `get_component_evidence` 调度、分页和关键状态保留、原始记录无损读回、跨任务与快照拒绝、来源变更拒绝、显式视图缓存隔离和归档读取不授予观察信用。
