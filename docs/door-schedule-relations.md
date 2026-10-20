# 门实例与选定门窗表的来源关系

`analysis/door_schedule.py` 实现确定性、本地的来源链：持久空间对象 → 已读源型号标注 → 本轮选定图纸中已读门窗表行。输出始终保留候选状态；型号相同不证明项目、楼层、版本或实际适用关系相同。它不执行规范、造价或优化判断，不调用模型或外部服务。

## 调用

1. 使用既有范围/几何读取及 `bind_spatial_object` 绑定实际原生实体。角色显式选择 `door`、`door_instance`、`door_opening` 或 `门`，语义角色仍为声明。将源型号区域放入 `label_region_ids`，并读取其原文证据。图例对象被拒绝。
2. 用户通过 `related_drawings` 选定门窗表图纸，在该图原文检索中定位表格 ID。`read_door_schedule_table` 使用现有 `SemanticService` 读取指定快照和表格；参数为 `snapshot_id`、`table_id`、`header_rows`、`row_offset`、`row_limit`。分页返回的 `next_actions` 始终保留目标快照。
3. 调用 `trace_door_schedule`：

```json
{
  "object_id": "spatial_object_…",
  "annotation_evidence_ids": ["ev_…"],
  "target_snapshot_id": "snap_…",
  "table_id": "table_…",
  "context_evidence": [
    {
      "dimension": "revision",
      "source_evidence_ids": ["ev_源图版本"],
      "target_evidence_ids": ["ev_表图版本"]
    }
  ]
}
```

`context_evidence` 可省略，支持 `project`、`floor`、`revision`。程序只从双方已读原文中的显式字段（例如 `项目：甲楼`、`楼层：一层`、`版本：A`）提取值。相同文本标记 `matching_source_text`，不同值或单侧多个值标记 `conflict`；无明确字段保持 `unconfirmed`。即使字段文本相同，其对该实例和该表行的适用关系仍未确认。用户选择的项目、楼层、版本标签单独保存为 `user_labels_not_source_identity`，不会升级状态。

## 约束与输出

- 目标快照必须属于当前图或本轮明确选定资料；知道一个快照 ID 不授予读取权限。
- 原生对象通过 `resolve_object_input` 重新核验当前范围、文件、几何路径及单位策略。
- 标注必须来自该对象已绑定的几何/标签实体，且属于同一布局及图框。原文须为完整型号或显式型号字段，不能从说明正文截取字符串来猜型号。
- 对象上已经读取的其他型号自动保留，不能通过只传一个证据 ID 隐藏冲突；已绑定但尚未读取原文的替代标注保留为待读并返回读取操作。
- 仅使用本任务 `read_table_semantics` 或 `read_door_schedule_table` 已读观察中的记录。必须存在明确 `code` 表头角色及型号单元格/表头来源，不把第一列、备注或同名文字直接当型号。
- 同一型号的所有已读行候选全部保留；不同型号、多个候选行、显式项目/楼层/版本冲突均明确列出，不静默选一个，不把其他楼层行值借给当前实例。
- 每条候选保留实例实体及路径、标注实体、双方快照及哈希、表格 ID、记录 ID、行号、型号单元格、完整行字段与表头证据、观察 ID。单位继续由语义表原文提供。
- `state` 分为 `candidate`、`conflict`、`table_not_read`、`model_not_found_in_read_rows`。后两者不表示目标文件/型号不存在。
- `coverage.read_pages` 仅记录真实读取页面；完整表格、整图与实例总数一律不由该链确认。未读表、表格分页、上下文缺口返回可执行 `next_calls`。

关系存于 `task.data.door_schedule_relations`。`relation_id` 绑定请求双方来源和证据上下文，后续表格分页更新同一关系；候选 `record_id`、`edge_id` 绑定具体原表行和实例。相同行不同表头解读保留为不同候选，重复读取同一行只补充观察 ID。

`read_task_door_schedules(task_data, DoorScheduleRead(...))` 是纯只读分页入口，可按关系 ID 或对象 ID 过滤，返回深拷贝；无需创建新模型会话，不增加读取信用。工具执行框架负责保存 task，HTTP 使用 `GET /api/tasks/{id}/door-schedule-relations`。

## 验证

```sh
.venv/bin/python -m pytest -q tests/test_door_schedule.py
```

测试创建两份真实 DXF，覆盖单行链、同型号两行和不同楼层、原文版本冲突、未选快照、未读表、未绑定对象、异实例同型号、隐藏替代型号、表头角色要求、过期哈希、稳定 ID、持久回读及真实工具调用的跨图观察来源。它验证来源链与拒绝条件，不代表真实工程图纸门识别准确率或工程关系验收。
