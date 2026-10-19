# 通用房间轮廓与面积口径

识别层输出可复用的轮廓和关系，不依赖房间用途或优化规则。实现位于 `cad_agent/geometry/contours.py`，与 `cad_llms_group` 无运行依赖。本次不修改该仓库。

## 数据与证据

`discover_rooms` 的完整证据包含共享 `contours` 库，每个空间候选通过 `geometry_model` 引用：

| 字段 | 含义 |
| --- | --- |
| `outer_contour_id` | 候选整体轮廓；不是默认确认的内墙面或建筑面积边线 |
| `interior_contour_ids` | 严格位于整体轮廓内部的闭合轮廓，包括嵌套及相交对象 |
| `topological_hole_ids` | 该围合面直接内环；优先完整线形图，不能等同应扣除空洞 |
| `face_variants` | 成对墙面图和完整线形图的内环方案，去重不丢弃方案差异 |
| `boundary_intersection_contour_ids` | 与整体边界接触或相交的轮廓，不能作为完整内部对象扣除 |

每个轮廓有稳定 ID、WCS 顶点、面积、周长、直接包含上级、相交边界 ID、源图元 ID、逐边证据及生成方法。嵌套按严格包含关系记录；多个不相容上级保留为列表，不强制归并成单一树。语义始终为 `unclassified`，后续组件可另行判定空洞、柱、井、设备等用途。

原始闭合图元的完整轮廓也会保留，避免相交对象经构面后只能看到碎片。闭合曲线保留细分近似及 `curve_approximation_m`（当前 0.001 m）和坐标量化容差；曲线原始参数可沿图元 ID 读取。未闭合曲线不会自动补成洞。曲线不参与当前房间外边界构面，识别仍保留 `curved_boundaries_not_discovered`，不能据此宣称曲墙房间识别完整。

每次发现绑定源快照、DXF SHA-256、布局、范围、明确单位或覆盖假设、算法版本及诊断。文字关联和室内种子避开所选拓扑直接内环。没有证据的内部对象不自动分类或扣除。

## 工具与 HTTP

1. `discover_rooms(layout_id, bbox, view_kind, …)`：返回候选和 `contour_summary.read_request`。
2. `get_room_contours(discovery_id, candidate_id, offset, limit)`：返回外轮廓及分页内部轮廓（每页最多 20），包括 `is_topological_hole` 和直接上级。HTTP：`POST /api/drawings/{sid}/rooms/contours`。
3. `measure_room_area(discovery_id, candidate_id, basis, exclusion_contour_ids)`：按明确口径计算。HTTP：`POST /api/drawings/{sid}/rooms/area`。

| `basis` | 计算 |
| --- | --- |
| `outer_shell` | 整体轮廓面积，不扣除 |
| `topological_face` | 整体轮廓减所选构面方案直接内环；内环中的岛属于其他面，不自动回加 |
| `selected_exclusions` | 整体轮廓减指定内部轮廓的几何并集；重叠和嵌套不重复扣除 |

例如整体 100 m²，内环 9 m²、4 m²，9 m² 内还有一个 1 m² 轮廓：外轮廓为 100，拓扑面为 87，只扣 4 为 96，同时扣 9 和其内的 1 为 91。没有隐含的楼地面、建筑面积、使用面积或任何法规计量规则。

面积结果使用 `basis_area_m2`、`shell_area_m2`、`excluded_union_area_m2`、`result_polygons`（外环及孔洞）并保留所选轮廓 ID；确认净面积 `area_m2` 仍为 null，状态为 `needs_review`。图形结果可为多面集合。坐标先平移并换算为局部米后运算，避免大 WCS 坐标导致数值抵消。曲线近似会传播到面积证据，不能将显示小数位当作工程精度。

跨快照/跨候选、未知轮廓、外轮廓自身、与外边界接触或相交的轮廓拒绝扣除。指定扣除最多 100 个，不要求内部对象彼此不相交。空选择等同不扣除。同一输入复用不可变证据记录；旧版本记录缺少轮廓模型时要求重新识别。

agent 的待核查面积结论继续使用 `unknown` 状态，不得借新工具升级成确认净面积。轮廓读取有顶点、逐边、来源/关系 ID 的截断标记；完整记录沿 `evidence_url` 读取，分页不改变计算证据。每次识别最多 4000 个独立轮廓，超限保留诊断并要求缩小范围。

## 界面与复核

“识别房间”→ 候选的“整体与内部轮廓 / 面积口径”可以查看包含层级、选择口径及勾选扣除对象。整体轮廓绿色、用途未确认的内部轮廓红色；计算后可回原图查看结果和孔洞。SVG 证据保留灰色源线形、绿色计算结果与橙色内部轮廓；背景超过 2000 条路径时有截断提示。

本次证据在 `artifacts/room-contours-20261015/`，重放命令：

```sh
.venv/bin/python -m scripts.validate_room_contours
.venv/bin/python -m pytest tests/test_room_contours.py tests/test_room_discovery.py tests/test_room_geometry.py -q
```

验证包含构造的多内环和嵌套、相交对象并集、接触外边界拒绝、任意旋转与大坐标、圆形闭合轮廓/开放弧、HTTP/agent 调用与答案状态，以及 AR030 已有原始 DXF 的消防水泵房局部回放。原始 DXF 单独渲染并叠加轮廓，面积另用导出顶点的鞋带公式复算；源文件与只读参考文件哈希重新校验。真实样例沿用毫米假设，工程边界和对象用途仍需确认，不能将此验证当作全类型房间召回率或净面积验收。
