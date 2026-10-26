# 文本锚点与元数据架构验证（2026-10-26）

实现已在 `feat/text-metadata-anchoring` 完成。核心修复提交 `dccd97e`；原有未提交代码独立保存于 `61406f2`，并发业务表格修复保存于 `2319a91`，随后显式集成。按用户选择，更晚的并发改动保留为 main 工作区未提交改动，不包含在本次已验证提交中。

## 实现

1. Agent 和来源记录复核共用短 ID、请求内逐字文本目录；相同文字的不同证据保留独立身份。
2. 注册、恢复和缓存共用快照内锚点校验，核对 Unicode 字符跨度、逐字引文、永久 ID 和定位字段；不跨版本自动重定位。
3. 按需求类型、操作和已观察工具组合元数据策略；完整保留表头、单位、条件及观测依赖。重复注释采用可还原的公共字段表示；预算先去掉可选答案模板，再按完整依赖组省略记录。

协议详见 [文本、元数据与证据锚点](text-metadata-anchoring.md)。

## 自动测试

- 完整 Python 回归：1990 通过、56 跳过，发现 3 项历史任务投影回归；这些失败是来源重复元数据挤出完整记录及旧归属字段的问题，随后已修复。
- 修复后的相关测试：233 通过、4 跳过，包含上述 3 个失败案例、上下文编解码、预算、业务表格和来源交付；没有未修复的已知失败。不是把首次全量运行记为全绿。
- 前端 Node：58 通过。
- 跳过项主要依赖未分发的历史图纸或可选环境；两条 Pillow 弃用提示与本次修改无关。

证据：[完整运行](../artifacts/text-anchoring-20261026/integrated-full-tests.log)、[修复后相关测试](../artifacts/text-anchoring-20261026/final-affected-tests.log)、[前端测试](../artifacts/text-anchoring-20261026/integrated-node-tests.log)。

## 核心实现冻结版本的真实图纸验证

| 样本 / 路径 | 结果 | 来源验证 |
|---|---|---|
| 经济技术指标，直接产品解析 | 8 类目标指标及 5 类停车分类与独立参考一致 | 102 条锚点与文本编码往返通过 |
| 楼栋一览表，直接产品解析 | 6 行、29 个指定单元格与独立参考一致 | 77 条通过 |
| 外墙面 4，当前解析器重新导入真实 DXF，auto | 7 次工具调用，任务 completed | 54 条通过 |
| 屋面 7，真实订阅 Agent | 8 个做法原文块全部引用并交付；保留单位和工程适用性缺口，任务 partial | 38 条通过 |

上述四次运行的全部 `cad_agent/` 源码哈希与核心冻结提交 `dccd97e` 对应；其后最新主分支集成的复验见下节。表格验证直接调用产品解析入口，不冒充 Agent E2E；预算遗漏和全部已登记引文的编码检查分开记录。

屋面 7 使用 `responses_subscription` / `gpt-6.1-sol`，4 次模型请求，服务报告输入 46691 token、输出 1376 token；`allow_fallback=false`，没有付费 API 回退，来源批读仍关闭。`partial` 来自如实保留未知单位适用性，不代表完整工程验收；原表所选行的 8 个做法原文块已逐字核查其覆盖和引用。

证据：[指标表](../artifacts/text-anchoring-20261026/final-metrics/summary.json)、[楼栋表](../artifacts/text-anchoring-20261026/final-schedule/summary.json)、[重新导入](../artifacts/text-anchoring-20261026/final-reimport-wall4/summary.json)、[Agent](../artifacts/text-anchoring-20261026/final-agent-roof7/summary.json)、[屋面原文覆盖检查](../artifacts/text-anchoring-20261026/final-agent-roof7/sample-acceptance.json)。各目录另存输入 / 代码哈希、原始结果、证据账本、观测及 `model-context.json`。

## 明确未完成的验证及边界

真正匹配来源记录路线的屋面 `review` 多次遇到共享订阅队列超时；最后一次为 `SIWC_QUEUE_TIMEOUT`，0 次推理请求、0 token，不能记为真实 review 通过。另一次指定 review 模式的复杂问法实际进入普通 Agent，已单独留档，没有混记为来源记录复核。共享名额由其他任务使用，未提高并发或中断其任务。

证据：[最后一次 review 排队结果](../artifacts/text-anchoring-20261026/final-review-roof/summary.json)。该历史记录的 `effective_path=agent` 是失败运行的旧报告默认值；`route=source_records`、错误码和 0 请求数才描述此次实际路径，报告脚本已修正失败时的默认值。

本次证据证明所列真实样本的读取、锚点与文本投影可用；不证明全项目工程适用性、全部问法完整率或 token 节省收益。源记录 review 的在线调用仍需在共享订阅名额空闲时补验。

## 最新主分支集成补验

在收尾期间，远程 main 新增 `acf97ab`（复杂表格工作流）。本分支已在 `6e2bcdc` 合并该版本，并于 `1ec2f71` 修复集成后的序号来源与表格编译输出兼容性问题。保留来源词汇与原生表格两种解析能力；复杂表格历史记录也接入统一锚点校验。表格编译文字必须逐块来自完整引文，不能把相邻单元格的 1 和 933 拼成 1933；序号仍不能用于数量计算。

- 最终兼容性回归：**370 passed、4 skipped**；包括新复杂表格、原有业务表格、数值切片、Agent、上下文及来源工作流。最新前端：**59 passed**。
- 最新产品代码重新验证：指标表 102 条锚点、楼栋表 77 条、DXF 重导入外墙面 4 的 54 条均通过；相同独立参考值检查继续通过。
- 屋面 auto：222 条来源锚点和目录编码通过，状态 partial，保留候选 / 适用性缺口。
- 真实 Agent 保存任务的离线恢复：38 条证据身份、引文与跨度全部不变，统一锚点和编码往返通过。这是当前代码的离线恢复检查，不是新的一次模型 E2E。
- 新复杂表格入口的真实本地复验：经济技术指标 129 格、建筑物一览表 71 格，完整分页及独立 gold 核验通过；本次未额外发送这两张表给模型。
- 上述五次本地锚点验证的全部产品源码哈希均与最新集成代码一致。在线 roof Agent 的证据仍来自上节的真实运行；review 队列限制仍然保留，没有伪造在线通过。

证据：[最终 Python 回归](../artifacts/text-anchoring-20261026/latest-main-final-tests.log)、[最终前端](../artifacts/text-anchoring-20261026/latest-main-node-tests.log)、[指标表](../artifacts/text-anchoring-20261026/latest-main-metrics/summary.json)、[楼栋表](../artifacts/text-anchoring-20261026/latest-main-schedule/summary.json)、[重新导入](../artifacts/text-anchoring-20261026/latest-main-reimport/summary.json)、[屋面 auto](../artifacts/text-anchoring-20261026/latest-main-roof-auto/summary.json)、[真实任务离线恢复](../artifacts/text-anchoring-20261026/latest-main-roof-resume-final/summary.json)、[复杂指标表](../artifacts/text-anchoring-20261026/latest-complex-metrics/summary.json)、[复杂楼栋表](../artifacts/text-anchoring-20261026/latest-complex-schedule/summary.json)。
