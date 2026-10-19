# 复杂几何任务修复与真实回归（2026-10-16）

> 后续进展：本文保留为第一轮历史报告。当前空间几何修复、实际测量与浏览器核查见 [后续修复报告](spatial-geometry-repair-20261016.md)。下文 9 项部分完成与旧版本测试数字不代表最新状态。

对应问题：[修复前七场景评估](complex-geometry-evaluation-20261016.md)。本次修改应用通用的任务、计算、结果集合、工具路由和上下文协议，没有按图纸名称、题目全文、预期面积或测试场景 ID 添加生产分支。

## 已实现的修复

1. **把任务落实为操作及实际交付物。** 规划显式声明面积、周长、算术、排序、筛选、高亮、几何发现、内部扣除、关系、尺寸、视觉等操作。原始要求保留；名称搜索、面积批次、计划文字不能代替其他操作。工具执行记录分别表达尝试与满足，最终逐项结果还可以因范围或语义未确认降为部分完成。模型不能通过声明“完成”补出缺失的测量结果。
2. **工具测量可继续计算，且能独立核对。** 增加 `analyze_measurements`，从保存的多边形本地计算面积、外环周长、极差、平均值、总和及排序。测量引用包含观测、对象、字段、单位、口径与集合；计算校验核对这些引用及操作数。排序同值按原编号稳定处理，聚合拒绝缺测和内部重叠；未知单位或追问改单位不能沿用旧物理数值。围合候选不自动升级为净面积。
3. **跨轮恢复真实成果并替换高亮。** `get_analysis_results` 检查同图纸、来源哈希和已发布父会话，恢复原编号、批次及可复用测量观测。完整历史批次保持不变；筛选结果使用单独的 `highlight_action`。新答案和保存任务恢复共用前端应用逻辑，实际清除未选高亮，而非只在文字里声称已筛选。历史图像不冒充本轮已看图。
4. **按能力及现有产物开放工具。** 几何、语义、房间路径都能获取图纸范围；缺失工具可以按注册名申请，无父会话时不能请求历史恢复。关系问题分为目标定位、局部关系、图像绑定及测量阶段，尺寸问题进入原生尺寸核查，无名称问题进入几何空间发现。实体观测保留原文、来源区域、布局及分页，避免后续只剩坐标而丢失目标身份。不同阶段只开放必要工具，避免反复加载所有工具定义。
5. **增加实际几何分析。** `check_dimension_chain` 根据原生尺寸界点、尺寸线位置和连续性选择总分对应的链，本地计算标注和及界点距离，交付实际定位路径；不把邻近文字任意相加。`inspect_enclosure` 检查已选轮廓内部闭合图元及原文候选，不自动把设备、基础当作穿楼板洞口。几何发现遇到复杂度限制后有有限的重叠子区检查，保留失败、未访问和跨区大空间的覆盖缺口。
6. **修复上下文与预算提前收束。** 明确几何操作进入待办后，读到标题不再意味着可以结束。预算由原始计划操作确定，不因操作完成而中途缩小；仍有时间、工具数和请求大小上限。上下文从不可变证据记录构建，去掉重复几何和诊断，尺寸及关系使用字段表保留读取值；保留最新推理、函数调用和结果配对。待绑定图像由来源校验过的图像注册表重新附带，实体标签保留，图像存在不再禁止文字历史压缩。探索之后预留最终交付及最多一次纠错，具体告知哪条结论的哪些数字不受引用支持；不放宽数值校验，也不在诊断中保存未经校验的原始回答。
7. **根据改写回归修复生命周期与范围匹配。** 模型的第一轮计划可能增加操作；运行循环不再提前固定规划前的较小轮数，避免已取得边界却没有最后计算和答复机会。允许“楼层名”与“楼层名＋平面图”等价，严格保留完整楼层名，不能把“一层”与“十一层”“地下一层”混同。

主要实现：`cad_agent/analysis/{tasks,operations,result_sets,grounding,focus,boundary_context,session}.py`，`cad_agent/components/dimension_chain.py`，`cad_agent/analysis/enclosure_inspection.py`，`cad_agent/geometry/tiled_discovery.py`，订阅运行时，以及 `web/{app.js,room-batch.mjs}`。

## 验证方法

- 真实模型统一为 `gpt-6.1-sol` / `siwc_sol` / `responses_subscription`，使用项目现有订阅适配器，关闭回退。未使用脚本答案替代模型输出。
- 每组预先保存问题和验收项。原七场景三张图纸不变；扩展包含平均值、反向排序追问，以及额外的 `11805.dxf`、`11799.dxf`。
- 回归在独立数据库运行，图纸来源版本核验后复用。每次保存请求、工具调用、答案、任务状态、用量和代码哈希。冻结快照隔离同工作目录的并行修改。
- 回归配置为每问最多 360 秒、28 次工具调用。应用增加环境配置入口；已核实原 8766 服务无运行中任务后重启，当前服务沿用原图纸库，采用相同的 360 秒、28 次上限。未配置环境变量时仍默认为 180 秒、16 次。
- 发布、任务字段、复合功能验收和独立数值检查分别记录。诚实回答未知不计功能通过；候选多边形计算正确不证明房间身份或工程净面积。
- 独立审计从保存的顶点重新计算面积和周长；从尺寸原文及界点独立复算标注和、差值与距离。审计脚本不调用生产测量函数。

## 自动化与界面检查

v4 的完整 Python 测试为 **692 passed、4 skipped**。随后工作目录增加了其他并行实验测试；最新全量运行是 **701 passed、1 failed、4 skipped**。唯一失败为 `test_room_boundary_revision.py::test_initial_and_repair_requests_reserve_other_slots`：另一个实验脚本更改共享模块的 `OUT` 后，测试读取了不存在的 `dynamic-room-nested-boundary-20261016/model-view.json`。该文件单独运行 **4 passed**，本次最后修改涉及的 `test_general_tools.py` 与 `test_complex_operations.py` 合计 **36 passed**。未修改其他实验脚本或隐藏该失败。

前端 Node 测试 **28 passed**，`web/app.js` 语法检查通过。普通沙箱下曾有 15 项因禁止绑定本地端口失败；允许临时回环端口后重跑。日志见 `artifacts/complex-repair-unit-tests-v4.log`、`complex-repair-unit-tests-v5.log`、`complex-repair-unrelated-isolation.log`、`complex-repair-ui-tests-v4.log`。

新增测试覆盖稳定排序、计算引用伪造、单位不兼容、重叠与缺测、来源版本、跨轮恢复、连续尺寸链与非连续反例、闭合设备不自动扣除、局部分区覆盖、上下文压缩、保存任务筛选高亮、规划后的轮数增长、一次纠错上限，以及简写楼层不会误匹配其他楼层。

真实浏览器恢复已发布筛选任务后，卡片仅显示原编号 **3、5**，SVG 高亮编号也只有 **3、5**；源批次仍保存全部 6 项。检查没有再次调用模型。证据：`artifacts/complex-repair-ui-20261016/verification.json`、`filtered-restored.png`。

反向排序的真实回答及 `ordered` 集合为 **1、6、2**；该次 v4 高亮动作保存的卡片顺序仍为 1、2、6。最终修复让高亮动作继承计算排序。使用该真实模型运行的原始批次做只读本地重放，已得到 1、6、2，完整历史批次仍为 6 项；这项后修复检查是确定性重放，不冒充新的模型运行。证据：`artifacts/complex-repair-selection-order-replay-20261016.json`。

## 真实回归结果

按每个场景最后一次实际模型执行取结果：**11/11 发布答案；严格复合功能仅 2/11 完成，9/11 仍部分完成。** 其中原七场景为 1/7 完成，扩展四场景为 1/4 完成。不能称为复杂几何能力已全部修好。

最后证据来自不同修复阶段：原七场景做过完整冻结运行；随后按实际暴露的根因定向复测，v4 执行扩展四场景，v5 再测关系路径。没有宣称最终 v5 一次性重跑了全部 11 项；任何新失败都没有用较早成功替换。反向排序卡片顺序的最后修改另做真实批次的本地重放。

| 场景 | 图纸 | 交付与缺口 | 功能验收 | Token |
|---|---|---|---|---:|
| [卧室排序及极差](/Users/vigorchen/Documents/WorkExplore/cad_opt_agent/artifacts/complex-repair-final-20261016/chat_a36d23b7d2c143249db3bab40cc652b9.json) | 18#楼1229_t3.dwg | 6 个围合候选；排序及 0.103601 m² 极差正确，高亮一致；整层覆盖尚未确认。 | 部分完成 | 54,983 |
| [最大两处连续追问](/Users/vigorchen/Documents/WorkExplore/cad_opt_agent/artifacts/complex-repair-final-20261016/chat_e5f78e6d44f244adac266e9e44c77411.json) | 同上 | 继承原批次、单位及编号，实际仅保留 3、5，两者均 10.432 m²。 | 完成 | 33,118 |
| [面积、周长、洞口及标注](/Users/vigorchen/Documents/WorkExplore/cad_opt_agent/artifacts/complex-repair-final-20261016/chat_6273012907eb4b1b96bc3aff669840a9.json) | 11794.dxf | 136.8504 m²、48.2 m；实际检查内部闭合对象，未把设备自动扣除；真实楼板洞口及同口径标注差仍缺依据。 | 部分完成 | 113,222 |
| [门洞数量、净宽及定位](/Users/vigorchen/Documents/WorkExplore/cad_opt_agent/artifacts/complex-repair-final-20261016-v5/chat_95d7646b63174cd0b38e7befa0822eb9.json) | 11794.dxf | 已定位双方文字、读取局部关系、图像及尺寸；未建立双侧归属，未完成净口边线绑定、测距和高亮。 | 部分完成 | 116,302 |
| [厨房与餐厅连通分类](/Users/vigorchen/Documents/WorkExplore/cad_opt_agent/artifacts/complex-repair-final-20261016-v5/chat_24fb32fa47a2456696204681f9a0bf4e.json) | (6审)D5#楼_t3.dwg.dxf | 定位左、中、右三组名称并查局部开口；仍无逐处墙/门/敞开分类、完整表格排除或判断边界高亮。 | 部分完成 | 102,380 |
| [连续尺寸链及界点距离](/Users/vigorchen/Documents/WorkExplore/cad_opt_agent/artifacts/complex-repair-final-20261016/chat_20eb2f6cc2d64f1cb618d31f7b2aec2d.json) | 同上 | 2000＋8000＝10000 mm，差 0；界点复算和三条定位路径成立；目标楼层子图归属仍为候选。 | 部分完成 | 52,779 |
| [无名称空间前三](/Users/vigorchen/Documents/WorkExplore/cad_opt_agent/artifacts/complex-repair-final-20261016-v4/chat_2914da516edb4577a54b4447295f05c0.json) | 18#楼1229_t3.dwg | 实际几何发现及四个成功子区；发现值可复算，但未完成家具/图框排除、前三处核验和高亮。 | 部分完成 | 112,936 |
| [改写：候选平均面积](/Users/vigorchen/Documents/WorkExplore/cad_opt_agent/artifacts/complex-repair-extensions-final-20261016-v4/chat_c6a6f889edc54ff389319e55c9b63484.json) | 18#楼1229_t3.dwg | 本地计算六个候选平均值 10.397466 m²；简写楼层可定位，未证明整层名称覆盖。 | 部分完成 | 48,848 |
| [改写追问：最小三处](/Users/vigorchen/Documents/WorkExplore/cad_opt_agent/artifacts/complex-repair-extensions-final-20261016-v4/chat_38afef472f2f4c35b9bbcad78d2c4ea7.json) | 同上 | 真实模型排序为 1、6、2；继承和选中集合正确。卡片顺序的后修复由该真实批次本地重放及测试确认。 | 完成（含本地顺序修复验证） | 24,672 |
| [额外图纸尺寸链](/Users/vigorchen/Documents/WorkExplore/cad_opt_agent/artifacts/complex-repair-extensions-final-20261016-v4/chat_2dc7fbb4ad0d49429ffd34351f54ab32.json) | 11805.dxf | 显示 150＋300＝450 mm；CAD 未取整差约 0.000003 mm，界点独立复算一致；端点与墙体关联未确认。 | 部分完成 | 109,700 |
| [额外图纸局部空间两处](/Users/vigorchen/Documents/WorkExplore/cad_opt_agent/artifacts/complex-repair-extensions-final-20261016-v4/chat_e1e3015127af4a8fad2e9907468f1de9.json) | 11799.dxf | 实际执行局部围合发现；所选区域未形成可核查候选，未交付面积、周长、排序和轮廓。 | 部分完成 | 108,436 |

最新 11 项证据的订阅 Token 合计 **877,376**。本轮所有开发、失败和复测执行累计 **3,252,700** Token，实际有模型用量的运行 38 次；占用锁而未发送的 0 Token 记录另保留。这是接口报告用量，不是费用或订阅额度百分比。完整清单见 [运行清单](/Users/vigorchen/Documents/WorkExplore/cad_opt_agent/artifacts/complex-repair-run-inventory-20261016.json)。

独立复算未发现已交付候选面积、周长、平均值、极差或所检尺寸链的数值不一致。尺寸链审计分别比较显示原文算术和未取整 CAD/界点算术，不把四舍五入的差异当成相同口径。源对象身份、净面积及目标范围仍需单独核实。

**剩余失败是实际功能缺口：** 门洞双侧空间归属和真实净口边线尚未形成可靠交付；厨房/餐厅尚不能逐处分类；无名称发现仍可能得到家具、墙带或其他闭合对象，且复杂区域和曲线覆盖不足。图像被发送也不意味着已完成视觉绑定和测量。本次没有将这些场景的诚实未知改标为成功。

完整机器可读结果见 [逐项验收摘要](/Users/vigorchen/Documents/WorkExplore/cad_opt_agent/artifacts/complex-repair-summary-20261016.json)。

## 可复现入口与原始证据

- 原七场景完整冻结运行：`artifacts/complex-repair-final-20261016/{plan,results,independent-audit}.json`。
- 上下文修复后的定向复测：`artifacts/complex-repair-final-20261016-v2/`。
- 改写及额外图纸：`artifacts/complex-repair-extensions-final-20261016-v2/`。
- 两个不可覆盖的源码快照：`artifacts/complex-repair-frozen-20261016/`、`artifacts/complex-repair-frozen-20261016-v2/`；各自保存 `manifest.json`。
- 轮数与上下文修复快照：`artifacts/complex-repair-frozen-20261016-v4/`，对应后缀 `-v4` 的复测目录。最终服务快照为 `artifacts/complex-repair-frozen-20261016-v5/`，关系路径复测保存在 `artifacts/complex-repair-final-20261016-v5/`；服务生产代码已逐文件核对与 v5 一致。`-v3` 仅为未执行的中间快照，不作为模型验收基线。
- 脚本：`scripts/regress_complex_repair.py`、`regress_complex_extensions.py`、`freeze_complex_regression.py`、`audit_complex_repair.py`。冻结副本中的运行脚本导入冻结生产代码，输出到独立结果目录。
- 开发期运行保留在 `artifacts/complex-repair-20261016/` 和 `artifacts/complex-repair-acceptance-20261016/`。没有删除预算失败、校验失败或答案不完整的运行。一次开发运行检测到其他工作新增 `vector_recognition_v6.py`，保留其 `code_changed_during_run=true`；后续正式批次使用冻结代码避免此类漂移。

重复运行会追加新记录而非覆盖旧失败。若订阅被其他进程占用，在模型请求前返回 `SIWC_BUSY`、0 Token；这些记录单独计为未执行，不计功能通过或模型能力失败。

## 本地交付

8766 健康接口已返回 `grounded-tools-v5.1-operation-contracts` 和实际运行上限；验证记录为 `artifacts/complex-repair-deployment-20261016.json`。默认模型选择保持原设置；本报告的真实回归使用页面可选的 `siwc_sol`，不能将这些结果泛化到其他模型。

重启同一配置的命令：

```sh
CAD_AGENT_PORT=8766 \
CAD_AGENT_DATA_DIR="$PWD/artifacts/hybrid-retrieval-20261015/data" \
CAD_AGENT_TIMEOUT_SECONDS=360 CAD_AGENT_MAX_TOOL_CALLS=28 \
.venv/bin/python -m cad_agent
```

先确认原服务没有运行中任务并停止原实例，再执行；不要同时打开第二个使用相同图纸数据库的应用实例。
