# 图框／子图、尺寸、墙—门洞—房间公共组件

实现位于 `cad_agent/components/`。复用原始 DXF、快照身份、`DrawingIndex`、原生实体遍历、空间索引、围合发现、双墙面和门垛闭合；没有接入帮图服务，没有执行优化规则或修改源图。

## 工具与使用入口

| Agent 工具 | HTTP | 范围与返回 |
| --- | --- | --- |
| `get_drawing_context` | `POST /api/drawings/{snapshot_id}/components/context` | 必填 `layout_id`；可选 `frame_id/bbox`；`view=frames/subframes` |
| `read_dimensions` | `POST /api/drawings/{snapshot_id}/components/dimensions` | 必填 `layout_id`；可选 `frame_id/bbox/unit_override` |
| `query_building_relations` | `POST /api/drawings/{snapshot_id}/components/relations` | 必填 `layout_id/bbox`；复用房间发现参数；`view=rooms/walls/openings/relations`；`linework_mode=auto/all` |
| `get_component_evidence` | `GET /api/drawings/{snapshot_id}/components/{record_id}` | HTTP 返回完整记录；Agent 使用 `record_id/view/offset/limit` 分页 |

三个查询均支持 `offset/limit`。所有运行时使用同一注册表；不要求图像模型或外部 API。界面在“浏览结构”中提供上下文、尺寸、当前视野关系入口，可定位来源并下载完整记录。未知单位的关系识别需要显式选择；尺寸读取可保留无物理单位的原始数据。

## 契约与来源

结果包含 `record_id/snapshot_id/source_sha256/component_version/layout_id/request/coordinate_space/unit`。对象有稳定 `object_id`；构件及关系保留支持 `entity_ids`；标题和图签字段还指向来源文字、实体、区域及位置。相同标题不按内容合并。图框、子图、尺寸、关系均不覆盖已有事实集合。

缓存绑定源快照、组件和几何版本、计算参数；分页不触发新的识别。跨快照或错误布局／图框拒绝。Agent 获得服务器登记的 `observation_id`，候选解释只接受 `derived/unknown/conflict`，不接受 `observed` 升级。记录可重读；大数组在模型分页中显式截断，完整数据仍保存在证据接口。

## 图框与子图上下文

- 复用已有图框 ID，补充旋转闭合矩形候选，不替换历史归属。
- 从原生文字识别平面、立面、剖面、详图、说明、表格标题，提取标题内楼层和附近同方向比例候选。
- 图签字段仅读取显式“图号：…”等标签内容；冲突值并存，不用邻近文字猜字段。
- 一个独立闭合边界内仅有一个标题时返回候选范围；多个标题共用边框或未找到边界时，`bbox=null`，保留标题锚点和 `subframe_extent_unresolved`。不按标题中点虚构切图范围。
- 比例与模型单位分开；图签比例不用于坐标缩放。跨文件和多图空间配准尚未实现。

## 尺寸标注

保留原始文字覆盖、原生标注块中的显示文字、DXF 存储测量字段（若存在）、`ezdxf.get_measurement()` 解析值、样式参数、独立定义点复算和端点附近直线候选。

`actual_measurement_basis=ezdxf_get_measurement` 明确“解析值”的来源，不能等同于无误的 CAD 设计值。真实图纸出现过对齐标注定义点距离为 2700、解析库值为 0；组件保留显示文字及各数值，并标记 `native_and_recomputed_measurement_conflict` 和 `status=conflict`，不静默择一。显式数字文字覆盖与样式缩放值不一致也标冲突。

独立复算覆盖二维普通方向的线性尺寸和对齐尺寸；角度、半径等可读原参数，但尚未全部独立复算。未知单位不生成米制结果。端点关联仅支持同布局、同高程附近的 LINE；不是 CAD 关联约束。尚不重建炸开的尺寸文字／线段，不实现完整标注排版、公差和负 `dimlfac` 语义。

解析器升级为 `native-dxf-v3.2-dimension-layouts`，保留仅含尺寸实体的布局。旧快照保持可读；重新导入按新解析版本生成新快照。

## 墙、门洞与房间关系

复用既有围合候选的逐边双墙面支持与门垛补线，生成 `wall_face_candidate`、`opening_candidate` 和 `room_candidate`，以及 `bounded_by / opening_in_candidate_wall / opens_to_candidate_space` 关系。墙面对象暂不代表完整墙身或材质。

`linework_mode=auto` 在存在墙／柱图层线索时追加一轮有界围合，排除家具等线形；图层为 0 的实例尽可能沿原 INSERT 路径读取继承图层。原全线形发现保留，筛选后的记录另存，记录纳入／排除数量和图层名称。没有墙图层线索时回退全线形；`all` 可直接对照全线形。图层命名是候选依据，可能遗漏非标准图层上的墙，不证明对象身份。

配对门洞保留宽度、平行面间距、两侧空间候选、支撑实体与竞争配对诊断。即使一侧空间未识别，也保留洞口并标未解决。初版不区分门窗；增量版按下文的符号与编号证据输出类型候选，证据不足仍为未知。未确认的房间 `area_m2=null`。柜体和窗台仍可能形成误候选；曲墙、缺失图元、范围截断、未确认平面类型等继续暴露。

竖向门洞补线的端点在既有容差网格上对齐，避免角度舍入导致端点不相接；该修正已合入当前发现版本 `room-discovery-v1.5-contours1.1`。此改动有竖向共享墙和无洞口反例测试。

## 验证

```sh
.venv/bin/python -m pytest -q tests/test_components.py tests/test_room_discovery.py tests/test_room_geometry.py tests/test_general_tools.py
node --check web/app.js
.venv/bin/python -m scripts.validate_components --output artifacts/components-validation-current-20261015
```

真实回放脚本只读历史归档数据库和 DXF，校验 SHA-256 后复制必要源产物到独立数据目录，不启动历史工作区。三个真实样本固定为 11794、11805、11809；上下文覆盖 Model 布局，尺寸和关系仅覆盖脚本记录的局部。旧解析产物保留原快照身份；新组件运行代码单独冻结并记录哈希。

初次回放在 `artifacts/components-validation-20261015/`，修正尺寸冲突和增加图层筛选后的探索回放在 `artifacts/components-validation-final-20261015/`。目视发现后两例旧局部实际是剖面、表格；保留这些结果，没有当作平面正例。

平面目视核对记录在 `artifacts/components-validation-reviewed-20261015/`，共享几何模块更新后的最终冻结回放在 `artifacts/components-validation-current-20261015/`，三幅叠加图字节完全一致：重新选择三个平面局部，并将上述剖面、表格作为显式类型的负例。后两张平面范围参考既有定位记录选取周边大范围，不传入历史面积、轮廓或规则建议；类型为人工选择，不能据此声称自动判断图纸类型。查看 `summary.json`、各组件 JSON、`review.html`、叠加 PNG、`visual-review.json` 和 `code-manifest.json`。

| 原始 DWG | 全 Model 子图标题候选 | 局部原生尺寸 | 解析／复算冲突 | 空间候选 | 墙面候选 | 洞口候选 | 关系 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `(3审)16-10#楼_t3(1)-理工院改后（建筑已修改完）.dwg` | 32 | 78 | 35 | 2 | 10 | 11 | 18 |
| `(6审)D1-D3楼_t3.dwg` | 15 | 31 | 10 | 1 | 4 | 1 | 4 |
| `(6审)D7-D8楼_t3.dwg` | 2 | 48 | 18 | 2 | 10 | 3 | 12 |
| 合计 | 49 | 157 | 63 | 5 | 24 | 15 | 34 |

49 个标题中仅 7 个得到单标题闭合边框候选。63 个冲突均为解析值与定义点复算差异；独立读取原生标注块的数值文字，并按样式缩放和精度核对，63 处显示值全部支持复算值（`dimension-display-verification.json`），不等于设计错误。三张源 DXF 的 SHA-256 前后相同；所有返回对象的源实体及关系两端均读回核对。该次初版回放没有形成两侧空间均识别的洞口关系；当时该分支仅在两个相邻房间的合成案例上通过，后续真实双侧验证见下文增量。房间净面积仍不确认。

完整本地回归：365 通过、4 跳过；随后组件与轮廓专项检查 17 通过。回放以冻结 `code-manifest.json` 为准，当前工作区文件另记 `implementation-manifest.json`，不将并行开发后的文件误称为逐字节相同的回放版本。网页验证包括上下文进入、子图定位、显示文字／尺寸冲突、洞口宽度与双侧空间候选、关系分页；截图在 `artifacts/components-ui-verification/`。这些是本地组件与 UI 验证，未运行真实模型问答、生产平台 E2E 或正式优化验收。

## 门窗区分与双侧关联（2026-10-15 增量）

组件版本 `drawing-components-v1.2.1-door-topology`，洞口推导版本 `opening-evidence-v2.1`。沿用原生图元、图层继承、围合及快照证据；增加的实现集中在 `components/openings.py`。

- `opening_type` 分为 `door_candidate / window_candidate / conflict / unknown`，原始 `kind=opening_candidate` 不变。开启弧须与径向门扇及墙面开口相符，不能仅凭圆弧、门窗图层或附近编号认定是门。原生 OCS 中心先转换到 WCS，支持旋转和镜像块。真实正例中的门符号就在 `A-WINDOW` 图层，说明图层不能决定门窗类型。
- 窗候选需要局部唯一分配的窗编号与平行窗框线共同支持；分段共线窗框合并计算覆盖率。门编号配合滑轨可形成推拉门候选。编号约定尚未与门窗表核对。门符号与窗编号冲突时保留 `conflict`，不建立通行边；仅有编号仍是 `unknown`。
- 墙／柱线形选择增加 `1col` 等常见 COL 命名。错位门垛使用门符号与两端可追踪的实际门垛路径；墙厚变化的洞口使用两端实际门垛和四处向外延伸的墙面支持。缺失门垛不补闭。补闭线明确记录为推导线，源图不变。
- 闭合空间可显式保留有双门垛支撑的开口边，以及长度不超过 0.1 m、两侧邻边均有平行墙面支持的实际墙体短折返。每条例外保存在 `supplemental_boundary_evidence`；没有整体降低墙面支持门槛，房间面积仍不确认。
- `faces` 保留洞口两面，`face_widths_m` 保留各自长度；`width_m` 为较小的墙面开口长度，**不是规范净宽**。`sides` 分别记录两侧墙面和三个室内探测点；边界覆盖与内部点同时成立才关联空间。孔洞、同侧重叠、多个空间、范围截断不能用最近房间替代。
- `adjacent_to_candidate_space` 只表达空间相邻。仅门候选且两侧各有唯一、不同空间、没有竞争配对时，输出 `passage_status=supported_door_between_two_spaces` 和 `door_connects_candidate_spaces`。窗和类型冲突保留相邻证据，但不建立通行关系。旧记录的 `opens_to_candidate_space` 仍可读取，新界面兼容两种记录。

固定真实正例为 `(6审)D1-D3楼_t3.dwg` 的 Model 局部 `[14500,-6500,20500,5000]`：物业管理房与上侧卫生间之间的门，墙面开口候选宽 1.15 m。物业管理名称来自原生文字；上侧卫生间用途依据洁具布置目视核对，并非原生用途标签。真实门的两侧空间通过闭合边界、双侧探测点和原生开启弧独立回读核对。另用理工楼原局部核对门／窗／未定洞口，并对 D1–D3 截断下侧房间的范围进行负例检查。

验证脚本 `scripts/validate_opening_relations.py`；冻结代码、源图 SHA-256、逐洞口证据、局部叠加图和测试日志保存在 `artifacts/opening-relations-final-20261015/`，入口为 `review.html`。这是两个真实 DWG 的指定局部验证，不是全图识别率或工程验收。双扇门组合、无编号推拉窗、曲墙、代理实体和完整净面积仍未覆盖。

增量最终回放：两张图纸局部合计 7 个空间候选、18 个洞口，其中 9 个门候选、4 个窗候选、5 个类型未定洞口；两张图各有 1 处经目视与原生图元回读支持的双侧门连接。理工楼第二处为主卧室与卫生间的错位门垛，开口两面长度分别 0.85 / 0.95 m。4 个窗候选均未产生通行边。完整测试 386 通过、4 跳过；覆盖门窗符号、错位门垛、双侧空间、缓存身份及已有组件回归。

## 复合门扇与推拉门窗（2026-10-15 第二次增量）

组件版本 `drawing-components-v1.3.1-composite-openings`，洞口算法 `opening-evidence-v3.1-composite-leaves`。复用同一原生图元、墙面开口、双侧空间及公共查询链路，组合几何集中在 `components/fenestration.py`。

- `operation_type` 与 `opening_type` 独立。前者为 `single_swing / double_swing / sliding / ambiguous_swing / conflict / unknown`。推拉面板不能单独判定是门还是窗；单扇、双扇符号并存时保留歧义。开启方式冲突禁止通行关系。
- 双扇要求两条原生弧各有径向扇线，闭合端会合、铰点间距与两扇半径之和吻合、两扇朝同侧开启。40–80 度的小角度成对平开也可能是窗，须另有门编号才能形成门候选；孤立小角度弧不能成为门。
- 安装在墙面开口外侧的双扇符号，需要原图框线连接两个铰点与洞口，偏置最多 0.35 m。保留 `frame_binding_entity_ids`、`frame_offset_m`、`symbol_span_m`，不把符号宽度、墙面开口宽度当作通行净宽。
- 推拉要求闭合薄面板、不同轨道上的正重叠，以及面板组覆盖洞口。支持闭合多段线和四边均有原生直线的炸开面板、四扇面板组。缺少封边、无重叠固定玻璃、同轨叠线及孤立平行轨线不成立。`sliding_panel_pairs` 为兼容字段名，条目可包含多于两扇的 `panels` 与 `panel_count`。
- 编号按原生文字估计范围与洞口投影分配，允许旋转文字插入点落在门垛外；竞争距离不足 0.1 m 不分配。常用编号首两位作为宽度一致性筛选，容差为 0.15 m 与编号宽度 25% 的较大者，防止邻近大门编号串到小门。这个编号约定未核对门窗表，不支持约定的项目可能因此漏关联，不会据此修改几何宽度。
- 在推拉几何相同的情况下，局部唯一门编号支持门候选，窗编号支持窗候选，无编号保留未定；窗框线加窗编号也只能确认窗候选，不能凭缺少开启弧声称是推拉窗。

真实回放脚本为 `scripts/validate_opening_types.py`。新增 D5 和 D7–D8 平面局部，并保留 D7–D8 门窗立面详图负例。D5 / D7–D8 显式使用 `max_opening_m=6` 以核对宽洞口；默认仍为 3 m。验证对象是归档真实 DWG 对应的 DXF 局部，原生线形预览不是完整 CAD 渲染。本轮不能给出全图准确率，也不构成平台 E2E 或工程验收。

本轮最终证据入口：`artifacts/opening-types-final-20261015/review.html`。4 张真实 DWG 的平面局部合计 15 个空间候选、51 条洞口假设：23 门、10 窗、18 未定；其中包含 2 组重叠假设、多出 3 条记录，不能把候选条数当作物理门窗数量。原有 2 处真实双侧门连接保留；新增 D5 与 D7–D8 没有新增经双侧空间证据支持的通行边。门窗立面详图在正确指定 detail 与故意指定 floor_plan 两种输入下均输出 0 洞口、0 空间。

上次 5 个未定中，D1–D3 外置双扇门、理工 TLM1624 推拉门、理工 C0715′ 窗这 3 处补齐身份依据；D1–D3 玻璃框和理工曲折外墙洞口这 2 处保留未定。固定挑战样本包含 41 个本轮按源线形及文字逐项核对身份的物理目标，身份一致候选从 26 增至 32，仍有 3 个身份未定、6 个未形成洞口候选，未出现门窗互分错误。样本并非盲测或全图普查；未定、漏检均未算成正确。另外记录 3 处可疑几何洞口，不与门窗身份错分混算。

尚未取得真实推拉窗正例；同一推拉面板几何的门/窗/无编号区分仅有合成正反例，真实窗负例未产生通行边。D5 的 2 处推拉门及 D7–D8 的 4 处门符号因墙面开口证据不足仍漏检；另有 2 处 D5 入户门和 1 处 D7–D8 小角度双扇门身份未定。精确坐标、源图 SHA-256 和分类前后值见 `manual-annotations.json`、`manual-audit.json`，不是独立人工验收标签。

冻结版本完整测试 433 通过、6 跳过，当前工作区组件与索引专项测试 57 通过。当前工作区存在并行的源图索引性能优化；完整回放与完整测试以 `code-manifest.json` 冻结版本为准，差异保存在 `workspace-comparison.json`。6 个回放范围均验证 HTTP、Agent 工具与完整证据读回一致；未调用真实模型或生产平台。
