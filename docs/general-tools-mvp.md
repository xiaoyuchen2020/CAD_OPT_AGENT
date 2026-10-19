# 通用图纸阅读与分析 MVP（2026-10-14）

本次仅修改 cad_opt_agent。原有砌体抽取、原生默认运行时、两个外部 Harness、模型选择配置及显式回退继续保留；未接入 AR024、规范判定、工程量测算或一次性代码执行。

## 已实现范围

| 优先级 | 实现 | 当前边界 |
| --- | --- | --- |
| P0 结构化阅读 | 概览新增布局/图框/章节/表格分页入口；多关键词 any/all 检索与章节范围筛选；上下文关系标记；read_table 通用表格读取 | 图框、章节、表头都保留候选状态；检索是文字匹配，章节归属依据解析器关系 |
| P1 通用事实 | GenericFact 容器；generic.key_value@1、generic.table@1 两个确定性抽取器；通用属性查询；按对象/属性/值/范围分别记录来源 | 保留为 needs_review；键值抽取不猜对象，表格对象与表头关系为 derived；未实现任意 schema 或模型生成抽取器 |
| P2 空间查看 | query_entities 查询文字/几何；保留类型、原始图层、块实例路径、块名和 bbox；inspect_region 返回附近图元及局部 SVG | 空间邻近不证明构件或引线关系；模型没有读取图像，视觉识图仍未接入 |

工具从六个扩展为九个，三种运行时共享同一个注册表和证据约束。新增工具没有 Shell、文件写入路径、任意代码或外部 URL 代理参数。

## 工具接口

- `get_drawing_overview(view="titles|layouts|frames|sections|tables", layout_id, frame_id, offset, limit)`：标题默认保持原接口；结构视图分页返回入口。
- `search_sources(query, keywords, match="any|all", layout_id, frame_id, section_id, offset, limit)`：返回实际命中词、匹配依据和区域入口。query 与 keywords 合并为检索词。
- `read_source_context(region_id, neighbors, start)`：复用原文分页，并区分 parser parent、child、sibling 与普通邻近；关系状态为 candidate。
- `read_table(table_id, row_offset, row_limit)`：先从概览 tables 视图取 ID，返回单元格原文、坐标、行列位置、横向跨度和逐段证据。首行只是表头候选；不自动继承单位或填充空值。
- `extract_facts(extractor_ref, frame_ids)`：注册项为 `building.masonry@1`、`generic.key_value@1`、`generic.table@1`。未知名称拒绝。通用事实最多 2,000 条，超限标明 truncated。
- `query_facts(fact_type, subject, property, query, frame_id, offset, limit)`：默认 `building.masonry.v1`，可选 `generic`、`generic.key_value.v1`、`generic.table.v1`。generic 会查询当前快照已存在的所有通用集合；只要键值结果时应选具体类型。
- `get_evidence(evidence_ids)`：读取当前快照下原文、砌体或通用事实证据，仍受每次最多八个来源限制。
- `query_entities(layout_id, frame_id, bbox, layer, kind, query, offset, limit)`：按原始 CAD 坐标相交查询；返回几何 bbox 和文字来源区域 ID；不返回所有曲线细分点给模型。
- `inspect_region(region_id, padding)`：受限为当前快照已有区域，返回局部预览 URL、文字证据及附近图元，固定声明 `visual_input_to_model=false`。

## 数据和证据

新增 `GenericFact` 含 `subject / property / value` 三个 FactValue、`scope_evidence_ids`、`evidence_roles`、`extractor_ref`、`relation_state` 和 `review_status`。来源均由程序绑定原图字符范围和实体 ID。事实分别存为 `generic_fact_set`，不覆盖 `fact_set` 或 drawing.latest_fact_set_id；原有砌体流程继续使用原集合。

最终结论区分两种依据：

1. `evidence_basis=source`：设计内容必须引用已读原文 evidence_ids；继续核对来源及数值；可用 evidence_roles 标记 subject/property/value/scope/relation。单个明确角色 ID 可规范化为一元素数组，仍检查已读状态和引用归属。
2. `evidence_basis=tool_observation`：行列位置、图层、图元类型、覆盖统计等只能引用工具真实返回的 obs_ ID。后端保留快照、工具和结构化数据，页面标为“工具观测，非原文结论”，并可展开查看。fact_set_id、region_id 不能充当 observation_id，也不能用观测 ID 替代原文引用。

该分离解决了旧校验器把“第2行”或图元路径数字误当设计数值的问题。它验证观测身份和数字来源，不是完整语义蕴含校验；不能宣称所有通过校验的结论均工程正确。模板要求设计材料、等级和适用范围继续引用原文。

## 页面与 API

页面左侧底部新增“浏览结构”，可切换章节、表格、图框和布局。表格按行浏览，点击任一文字来源可定位原图；证据抽屉新增“打开局部预览”。问答工具记录显示新工具中文名称，工具观测与原文引用分开展示。

新增只读端点：

- `GET /api/drawings/{snapshot_id}/structure?view=tables`
- `GET /api/drawings/{snapshot_id}/tables/{table_id}`
- `GET /api/drawings/{snapshot_id}/sources/{region_id}/inspect`
- `GET /api/drawings/{snapshot_id}/sources/{region_id}/preview.svg`
- `GET /api/generic-fact-sets/{fact_set_id}`

解析器版本更新为 `native-dxf-v3.1-general-geometry`。旧图纸快照和旧证据不重写；重新导入同一 DXF 会生成新快照，保留完整几何元数据与新章节解析。原图 hash 不变，版本化 snapshot ID 会改变。

## 验证方法与产物

```sh
python -m pytest -q
node --check web/app.js
python -m scripts.check_general_tools --output artifacts/general-tools-local
python -m scripts.check_general_tools --live --output artifacts/general-tools-live
```

脚本使用既有四份真实 DXF；源路径只读，按既有 SHA 验证，再复制导入本仓库隔离数据目录。目录名中的历史规则标签仅属于样本来源，不导入规则代码。

- 最终自动回归：112 项通过，`node --check web/app.js` 通过。自动回归覆盖跨快照拒绝、章节不跨同级范围、空单元格、横向合并、表头不自动继承单位、单元格字符范围、块路径、bbox、SVG 转义、通用事实隔离/缓存、证据角色及工具观测隔离。
- 最新解析器的四份真实样本结果在 `artifacts/general-tools-v31-20261014/`。候选表格数分别为 34、35、68、68；总计核对 11,805 个表格来源片段的原文范围与实体归属。这是来源绑定检查，不是表格语义正确率或独立表格数。
- 首次真实模型验证在 `artifacts/general-tools-20261014/`，保留表格行列序号和图元元数据被旧答案契约拒绝的记录。
- 工具观测契约修正后的防水、保温、表格、局部区域问答，以及两种 Harness 和两项原砌体回归，在 `artifacts/general-tools-final-20261014/`。这些记录体现开发过程，不能跨版本拼成一次统一成功率。
- 最新解析器的通用事实与防水问答复测（包括失败候选和再次修正结果）在 `artifacts/general-tools-v31-20261014/` 及其 `retest/` 子目录。最终两项复测均发布并通过来源检查；此前失败保留且未发布。模型仍可能多答、误解范围或未按要求引用。例如最后一条防水回答将原文的特定部位等级称作“加严要求”，这一比较关系并非原文明确结论，不能因来源检查通过就认可该解释。
- 网页额外用现有 `20240729-建筑_设计说明_t3.dxf` 的电梯参数表验证了单元格读取、原图高亮与局部 SVG；截图为 `artifacts/general-tools-final-20261014/ui-table-evidence.png`。该网页样本不计入四份固定输入统计。

## 未完成的视觉能力与其他限制

局部 SVG 是供人查看的矢量预览，当前模型通道仍只接受文字与工具调用，未把图片发送给模型。若要模型识别平面构件或节点形状，仍需接入明确支持图像输入的模型与相应协议适配，并验证图像坐标回绑。当前不能宣称具备视觉识图。

通用表格先覆盖具有可识别水平/垂直边线的表格；跨行合并、多级表头、无边线表格、旋转表格、同格多项对应关系仍需核查。预览有字体替代及图元支持限制，局部 SVG 最多绘制 2,000 个线图元和有限文字，不保证完整 CAD 显示。块名仅保留可追溯的 INSERT 名称，不等于构件分类。没有新增跨文件关联、OCR、工程量、规范或优化结论。
