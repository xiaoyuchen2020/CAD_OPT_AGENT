# 经济技术指标与建（构）筑物一览表提取

两个只读入口复用现有 CAD 表格、来源记录缓存和原文校验，不调用模型、不计算缺失项、不改动图纸。

| 入口 | 返回内容 |
| --- | --- |
| `extract_planning_metrics` | 经济技术指标字段、原始指标名称、设计/控制值、分项限定、数值及单位 |
| `extract_building_schedule` | 按原表行组织的建筑记录、身份来源、字段；合计/小计行单独返回 |

## 共享读取与业务适配

`DrawingIndex` 表格候选 → `read_table_page` 规范语义页 → 两个业务适配器 → 字段及证据。

`read_table_page` 复用 `source_record_store.verify_sources`、来源签名和摘要校验机制。缓存绑定快照、regions/scene 内容哈希、语义解析器版本和请求参数，保存原始语义页，不保存业务结论或已读信用。读取前后核对索引签名，返回深拷贝；证据字符范围逐项回源验证。

业务适配器使用明确的原表标签和表头路径匹配字段。标签归一只用于字段分类，原始名称保留，例如建设用地/净用地、绿地/绿化、不同停车位类型不会因字段分类相同而自动合计或确认口径等价。

## 调用

HTTP：

- `POST /api/drawings/{snapshot_id}/business/planning-metrics`
- `POST /api/drawings/{snapshot_id}/business/building-schedule`

Agent 工具使用同名 Python 入口；各运行时共享工具注册、执行、证据及观测登记。两个工具属于 sources 和 semantics 组。

请求 `{}` 只发现当前快照的候选表，返回 `candidates`、`matched_fields`、证据、`suggested_header_rows` 和下一步调用。不会自动选择唯一候选，也不会把没有候选解释成图纸没有数据。可用 `layout_id`、`frame_id` 限定范围；表格必须完全包含在指定图框中。

Agent 可通过 `snapshot_id` 读取本轮用户已选关联图纸，未选快照会被拒绝。来源及分页保留目标快照，不跨图合并字段。HTTP 请求中的 `snapshot_id` 若提供，必须与 URL 中的快照一致。

已知表格后，例如：

```json
{
  "table_id": "从候选结果取得的实际 table_id",
  "header_rows": [0, 1],
  "requested_fields": ["name", "height", "counted_area", "excluded_area"],
  "row_offset": 0,
  "row_limit": 5
}
```

`header_rows` 可省略，程序仅按明确字段标签推断从第 0 行开始的连续表头，最多四行。不确定时返回 `HEADER_UNRESOLVED` 和原表读取入口，不能猜数值列。显式选择表头也不证明整个表格正确。

`row_offset` 包含表头行。使用返回的 `next_actions` / `next_offset` 续读；实际页面可能受语义读取的响应大小限制缩短，不能自行加上请求的 `row_limit`。候选发现使用独立的 `offset` / `limit`。

Python：

```python
from cad_agent.analysis.business_table_contracts import PlanningMetricsRequest
from cad_agent.analysis.business_tables import extract_planning_metrics

result = extract_planning_metrics(
    workspace, snapshot_id,
    PlanningMetricsRequest(table_id=table_id,
                           requested_fields=["land_area", "floor_area_ratio"]),
)
```

## 字段

经济技术指标：

| 字段 | 含义 |
| --- | --- |
| `land_area` | 用地面积，保留原始用地口径 |
| `total_building_area` | 总建筑面积/建筑面积，保留原始名称 |
| `counted_area` / `excluded_area` | 计容/不计容建筑面积 |
| `green_area` / `footprint_area` | 绿地/基底面积 |
| `floor_area_ratio` | 容积率 |
| `building_density` / `green_ratio` | 建筑密度/绿地率 |
| `parking_spaces` | 停车位数量，保留机动车/非机动车等限定 |

建筑一览表：`ordinal`、`name`、`use`、`storeys`、`height`、`total_building_area`、`counted_area`、`excluded_area`、`footprint_area`。地上/地下子表头放入 `qualifiers`，不把两列层数视为冲突；未明确写出的合计层数不计算。

`requested_fields` 省略时检查本入口全部字段。字段缺失不填零。指标支持纵向“项目—数值—单位”、设计/控制值多列，以及横向指标表头布局。

每个字段保留 `raw_text`、精确十进制字符串 `value`、比较/约数标记 `qualifier`、单位及单位来源、表头路径、原始标签、合并跨度、单元格坐标和证据。支持显式面积单位、%、长度单位和计数单位；层数/停车位可由明确计数标签得到“层/位”，用 `unit_basis=count_label` 标识。不做单位换算。范围、表达式等非单值保留原文并标为歧义。

状态为 `observed`、`missing`、`ambiguous`、`conflict`。`observed` 仅表示该字段由明确来源解析，不确认工程身份或图纸正确性。相同来源键的不同值保留为冲突，不自动选新值或求和。

## 分页、完整性与证据

- `page_state=complete`：本次返回页已读取。
- `fields_complete`、`missing_fields`、`unresolved_fields`：仅针对当前返回页及请求字段。
- `table_read_complete`：单次请求是否从第 0 行读到表尾；跨页 API 调用方需累计实际返回区间。
- Agent 的 `read_coverage`：只累计同一会话、同一快照/表格/表头/字段集合的真实已读页。中间缺页或只读最后一页不能得到整表完成；同时检查跨页指标冲突。
- `scope_exhaustive=false`：任何上述完成状态均不证明全图或项目资料穷尽。

合并单元格的连续行保留同一 `cell_id` 和 `continuation_of`，不把重复显示当成新建筑身份；普通空格不向下填充。建筑记录的 `engineering_identity_state` 始终为 `unverified`。

工具结果登记到既有证据和观测账本，`query_property_evidence` 可查询字段来源。比较/约数值在字段证据投影中保留原表达式，不能当成精确等式使用。

## 与来源记录工作流的关系

明确涉及指标/一览表的读取请求进入 `business_tables` 路由，给普通 Agent 提供两个业务工具的调用入口。路由保留全部原始分项和限制，不自动完成用户要求，也不会将未执行的路由提示算成已读证据。现有做法的固定原文编译工作流保持独立。

“提取总建筑面积”可以进入业务读取；“计算容积率”“测量基底面积”等仍走普通分析及计算流程。没有启用来源记录工作流或明确选择 Agent 模式时仍可直接使用两个工具。

## 当前边界和验证

范围是现有索引能够恢复的有线框、原生 CAD 文字表格。不新增扫描图 OCR、旋转表恢复、无网格文本重建或跨图工程身份匹配。表头别名以代码中明确列表为准；未支持的列保留为未映射单元格，无法归属的行保留在 `unresolved_rows`。图纸备注及特殊口径仍需要 Agent 核查。

测试：`tests/test_business_tables.py` 使用实际生成并导入的 DXF，覆盖计容否定词、百分比、设计/控制值、合并表头与身份、缺失与冲突、分页、缓存、横向指标、原文校验、Agent/HTTP 入口及字段证据查询。它们是解析和集成回归，不是客户真实图纸或真实模型 E2E 验收。
