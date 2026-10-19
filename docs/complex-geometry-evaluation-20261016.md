# 复杂几何问答实测（2026-10-16）

本轮在此前用户授权的三张图纸上，预先固定 7 个复杂场景并逐一执行真实模型测试。模型为 `gpt-6.1-sol`，运行时为现有 `responses_subscription` 订阅适配器，关闭回退；每个独立场景仅执行一次，筛选追问使用第一组真实回答作为父会话。没有修改生产代码、替换模型答案或为了通过题目追加模板分支。

**完整功能验收 0/7。** 其中 2 项有部分有用交付，3 项在进入实际几何分析前结束，2 项答案校验失败。5/7 发布了回答，但发布成功不代表任务完成。全部运行报告的订阅 Token 合计 **324,975**；该数值不是费用或订阅额度百分比。

## 逐项结果

| 场景与图纸 | 实际执行与交付 | 功能验收 | Token |
| --- | --- | --- | ---: |
| 一层卧室面积排序与最大最小差；18#楼1229_t3.dwg | 6 个轮廓、6 个面积、降序排序正确；没有交付面积差 | 部分完成 | 52,785 |
| 连续追问：只高亮最大的两处，同面积按编号；同图 | 只规划及两次扩展工具；未恢复上一轮批次，也没有交付筛选后的高亮集合；答案未发布 | 失败 | 32,399 |
| 水泵房面积、周长、仅扣证实洞口、原图核对；11794.dxf | 围合候选面积 136.8504 m²，说明原图面积文字差异；没有周长，没有执行洞口专项识别/扣除分析 | 部分完成 | 43,818 |
| 水泵房通向天井的门洞数量、最小净宽、标注对照、测距边线；11794.dxf | 只搜索及读取两段文字，没有局部看图、门洞识别或测距；回答均为无法确定 | 未实现目标 | 37,481 |
| 二层各厨房与餐厅的敞开/门洞/隔墙关系；(6审)D5#楼_t3.dwg.dxf | 做了房间候选和局部图核查，排除了表格名称，但没有交付逐处连通关系；答案校验失败 | 失败 | 55,760 |
| 二层下侧水平尺寸链：分尺寸求和、总尺寸对照、几何界线距离；同图 | 仅扩展工具、搜索、概览、读取楼层标题；未调用尺寸读取或几何测量 | 未实现目标 | 58,406 |
| 一层无名称空间发现、已检范围面积前三、排除家具表格图框；18#楼1229_t3.dwg | 只读标题并两次选择工具组；未调用 `discover_rooms`，未生成空间或面积 | 未实现目标 | 44,326 |

所有场景都明确要求保留候选性质或交代缺口。没有因为模型诚实说“不知道”就把功能判为通过，也没有因为任务字段为 partial 就忽略实际完成的有效部分。

## 可独立复核的证据

第一组的 6 个候选多边形均有效，彼此无内部重叠。用保存的顶点独立计算面积，与工具保存值在容差内一致。排序按工具保存精度为：

| 原编号 | 候选围合面积（m²） |
| --- | ---: |
| 3、5 | 10.432 |
| 2、4 | 10.431998 |
| 1、6 | 10.328399 |

最大减最小应为 **0.103601 m²**。模型第一次试图交付差值时，校验返回“计算结论必须为 derived 且引用原文，不可冒充 observed 或工具测量”；纠错后改为无法提供面积差。此处是测量结果不能继续运算的问题，而非缺少操作数。

第二组按原始计算精度应选编号 **3、5**。新结果没有这两处对象的筛选集合，也没有替换高亮的动作记录。不能把上一轮原有 6 处高亮算成这次筛选成功。前端保留历史画面与新结果是否交付也必须区分。

水泵房仍只能解释为带闭合假设的围合候选。原图解析有“面积154.05m”，平方上标在独立文字中；本轮回答没有把不完整单位补成平方米，也没有把设备和基础当洞口直接扣除。这些防止错误结论的行为正确，但不能替代周长与洞口分析。

六卧室局部图已经逐格目视复核，所选线围绕房间而非床框，开口缺口仍可见。保存图像哈希与记录一致。此项核对只支持本批候选，没有建立全图人工房间分割真值或工程净面积验收。

## 根因与修复优先级

### P1：任务完成条件仍按粗粒度类型判断

[TaskLedger.bind_batch](/Users/vigorchen/Documents/WorkExplore/cad_opt_agent/cad_agent/analysis/tasks.py:213) 将一般 measure 要求统一绑定到房间面积批次。只算出围合面积，就会把“面积差”“面积及周长”“扣洞后面积”等要求标为 delivered；`pending_work` 只认识批次边界审查及单位检查，不认识周长、聚合、扣除、连通和尺寸链。

另外，constraint 只要遇到已解决的单位就会完成，导致第一组“不要把候选面积说成确认净面积”也以“已采用毫米”作为完成理由。约束类型尚未区分单位、计量口径、对象排除和排序规则。

应将每个要求落实为可检查的对象、操作、输出及证据条件。例如 perimeter 必须有同一轮廓的长度结果；difference 必须有两个兼容口径的测量引用及可复算差值；筛选高亮必须有实际两项集合。不能仅按文本分项或通用 measure 标签交付。

### P1：计算只支持原文数值，不支持工具测量结果

[verify_calculations](/Users/vigorchen/Documents/WorkExplore/cad_opt_agent/cad_agent/analysis/grounding.py:33) 要求 `evidence_basis=source`，操作数必须引用原文字段。工具计算面积无法作为后续算术、排序筛选、单位换算的正式操作数。

应增加受控测量引用（观测 ID、对象 ID、字段、单位、口径），由本地程序执行差值/聚合/排序/筛选并返回新观测；保持来源核验，而不是放宽任意数值检查。

### P1：连续追问缺少可操作的上一轮成果

[chat_history](/Users/vigorchen/Documents/WorkExplore/cad_opt_agent/cad_agent/service.py:396) 只传问题与回答文字。新任务没有恢复上一轮对象集合、轮廓、编号与测量观测。当前工具也没有读取历史批次并替换高亮集合的接口。

本次模型正确识别了缺少这些能力，却只能连续请求工具组，最终因为没有当前任务数据观测而校验失败。应继承可验证的成果引用，并提供集合筛选与明确的高亮替换动作；无法执行时应能交付明确能力缺口，而不是再次陷入格式纠错。

### P1：工具路由与预算在关键几何动作之前收束

[工具组定义](/Users/vigorchen/Documents/WorkExplore/cad_opt_agent/cad_agent/analysis/focus.py:13) 将 `get_drawing_context`、`read_dimensions`、`query_building_relations` 放在 components。无名称空间场景为了获得 `get_drawing_context`，连续扩展 geometry 和 semantics，两个都不包含目标工具。

门洞与无名称场景触发 `subscription_answer_reserve`；尺寸链触发 `subscription_exploration_budget`。[停止逻辑](/Users/vigorchen/Documents/WorkExplore/cad_opt_agent/cad_agent/runtimes/subscription.py:197) 在已有文字证据、累计达到 30k 且 `pending_work` 为空时收束；40k 则停止探索。当前 pending_work 无法表达这些几何操作，所以读了标题或房间词就可能失去继续几何分析的机会。

优先修正能力映射、按阶段暴露必要工具、压缩重复上下文，并为明确几何步骤保留执行预算。单纯放大全局 Token 上限不能解决缺少操作与工具组映射的问题。

### P2：以实际交付物验收，区分诚实未知与功能成功

尺寸链及无名称发现答案虽成功发布，实际没有发生目标几何分析。厨房连通被主要路由到面积/轮廓流程，尚未建立用于分类的墙/开口关系证据。验收应检查所需工具结果、对象覆盖及可视化动作，不能用“已调用工具”或“已发布回答”代替。

## 复现与审计记录

- [预先定义的场景和验收条件](/Users/vigorchen/Documents/WorkExplore/cad_opt_agent/artifacts/complex-geometry-20261016/plan.json)
- [全部真实运行结果](/Users/vigorchen/Documents/WorkExplore/cad_opt_agent/artifacts/complex-geometry-20261016/results.json)
- [独立面积、高亮、重叠及图像完整性检查](/Users/vigorchen/Documents/WorkExplore/cad_opt_agent/artifacts/complex-geometry-20261016/artifact-audit.json)
- [人工逐项验收与根因](/Users/vigorchen/Documents/WorkExplore/cad_opt_agent/artifacts/complex-geometry-20261016/review.json)
- [运行 ID、Token 和代码目录变化摘要](/Users/vigorchen/Documents/WorkExplore/cad_opt_agent/artifacts/complex-geometry-20261016/summary.json)
- [真实测试脚本](/Users/vigorchen/Documents/WorkExplore/cad_opt_agent/scripts/regress_complex_geometry.py)、[只读复核脚本](/Users/vigorchen/Documents/WorkExplore/cad_opt_agent/scripts/audit_complex_geometry.py)

每次运行保存请求、工具调用、结果、用量及开始时的代码哈希。已有生产 Python 文件在本批开始至结束间哈希没有变化；水泵房组合场景期间检测到目录新增 `geometry/vector_recognition_v4.py`，该场景保留 `code_changed_during_run=true`。没有观察到既有代码引用此新增文件，也没有把这次目录变化隐藏成严格冻结基线。本轮未修改该文件或其他生产逻辑。

测试使用独立数据库，未重启或修改 8766 服务，也没有写入正式图纸库。报告中的 0/7 针对本批严格复合要求，不表示底层几何工具完全不可用，更不能将未执行的几何检查解释为图纸本身无解。
