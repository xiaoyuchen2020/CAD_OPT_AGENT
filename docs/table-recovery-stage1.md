# 表格恢复第一阶段：固定方法与隔离验证

2026-10-16。实现位于 `cad_agent/extensions/table_recovery/`。这是独立的固定方法试点，不接入 LLM 动态代码、Agent 工具循环、业务规则或原图写入。现有 `read_table`、`read_table_semantics` 和失败行为保持不变。

## 使用范围

只处理已有 `table_id` 范围内的轴对齐原生直线网格。`anchored-lines@1` 复用既有行列锚点，以线段覆盖判断分隔；有限短缺口允许桥接，部分分隔存在竞争解释、非矩形合并、外边界无支撑时保留未解决。没有无边线表格、OCR、跨表继承、旋转表格和新候选发现能力。

参数由服务端固定：坐标容差由字号和最小格距有界计算，短缺口总长不得超过边长的 2% 或坐标容差。不能通过接口调整容差、增加依赖或更换代码。所有通过自动检查的网格仍为 `state=candidate`、`review_status=pending`、`semantic_acceptance=false`。

## 输入与来源

诊断接口另行调用现有表格组件，保留真实异常；不会改变旧接口的错误包装。冻结输入记录源 DXF 哈希、解析产物哈希、快照和布局、实体实例路径、解析/索引/表格/几何版本、原生直线端点、文字和原文证据、既有网格锚点、失败诊断及排除项。

线段来自 `native_source`，不使用预览曲线离散结果。首期只纳入 Z=0 平面 LINE 和不含圆弧的二维多段线；其它几何保留排除记录。文字仍来自当前解析产物，定位框为估计范围，不能作为精确字形边界。已知解析缺失、未知位置的几何、旋转文字等会阻止固定方法自动给出网格；不会根据“看起来在远处”丢弃无法定位的代理实体。

`input_id` 为规范 JSON 内容哈希，读取和执行都会重新校验。运行前重新准备输入并核对哈希；源文件在执行期间变化也使运行失败。完整输入存为独立记录，分页读取不会改变输入。原始 DXF 不进入容器，容器只通过标准输入获得不超过 8 MiB 的局部 JSON。

## HTTP 接口

前缀：`/api/drawings/{snapshot_id}/table-recovery`。沿用工作台现有本地 Host/Origin 检查。接口暂未加入 Agent 工具清单。

| 接口 | 请求 / 返回 |
| --- | --- |
| `POST /diagnostics` | `{"table_id":"..."}`；返回 diagnostic/input ID、原组件状态、覆盖和固定方法 |
| `POST /inputs/read` | `input_id, view=texts/segments/excluded_geometry, offset, limit`；最多100项 |
| `POST /runs` | `diagnostic_id, input_id, method_ref=anchored-lines@1, idempotency_key`；有界同步运行 |
| `GET /runs/{run_id}` | 执行状态、验证状态、结果和执行记录 ID、镜像摘要、耗时 |
| `POST /results/read` | `result_id, offset, limit`；返回已绑定原文证据的候选单元格 |
| `GET /results/{result_id}/overlay.svg` | 可信渲染器绘制原图和橙色候选网格，不渲染方法生成的 HTML |

第一阶段采用同步 200 响应，不宣称已有异步 202、取消任务或人工审批 API。UI 可直接查看生成的原图复核报告。所有请求禁止额外字段；`source`、动态方法名、文件路径、镜像名都不属于接口。

任务状态与验证状态分开：`execution_status=completed` 只表示进程结束；验证为 `passed/needs_review/rejected`。未解决结果没有格子，不能标为通过。相同幂等键和请求返回同一记录，键相同但内容不同拒绝。每个输入最多三次不同运行，已有运行累计达到120秒后不再启动下一次；工作台同时只执行一个恢复任务。单次容器30秒墙钟、20秒CPU、512MiB内存、32个进程、64MiB临时空间、2MiB输出；没有主进程执行回退。

## 独立验证

验证器不导入固定方法，也不执行它返回的函数。使用 Pydantic 严格契约、Shapely 线段覆盖/几何并集和独立的槽位检查：

- 输入身份、行列锚点、范围和单元格跨度合法；网格无重叠、缺格或越界。
- 每条边引用真实源线段，并重新检查共线、重叠和覆盖。
- 存在完整原生分隔的格子不能被合并；“全表塞进一个大格”不能绕过检查。
- 文字集合完整且不重复，按原点检查分配；虚构、丢失、串格和重复文字均拒绝。
- 单元格文字、引用与 ID 由宿主重新绑定；不接受代码返回的原文、证据、单位或数值字段。
- 字体估计框跨格、排除几何等保留警告；自动通过不证明表格身份、阅读顺序或工程语义正确。

拒绝结果不保存可用候选格子。原始进程输出仍以独立执行记录保存，便于审计。运行、执行、结果使用不同 ID，适配现有全局记录主键。

## 隔离环境准备

需要已运行的 Docker Linux daemon；本次验证使用本机 Docker Desktop 的 Linux VM。使用容器无网络、无宿主挂载、只读根目录、非 root、丢弃全部 capabilities、no-new-privileges、固定入口和镜像内容摘要。它不是新建的专用 VM，也不代表已验收任意不可信代码执行。

先拉取官方基础镜像，再在仅含 Dockerfile/worker.py 的临时上下文构建：

```sh
docker pull python:3.11-slim
.venv/bin/python -m scripts.setup_table_recovery_sandbox --data-dir .data
```

构建记录实际基础镜像 RepoDigest、固定方法 SHA-256 和最终镜像 ID。应用只按 ID 使用本地镜像，禁止运行时拉取/构建，源码变化会使旧配置失效。容器没有 `.env`、用户目录、图纸目录、Docker socket 或网络。无沙箱返回 `SANDBOX_UNAVAILABLE`。

固定维护探针验证非 root、根目录只读、宿主文件不可见、网络不可达和凭据环境缺失。超时与输出超限探针检查整个容器清理；这些维护操作不暴露为 HTTP 方法。资源配置已记录；未通过真实 OOM 压力测试的部分不能声称完成压力验收。

## 验证与证据

```sh
.venv/bin/python -m pytest -q tests/test_table_recovery.py tests/test_semantics.py tests/test_general_tools.py
.venv/bin/python -m scripts.setup_table_recovery_sandbox --data-dir artifacts/table-recovery-replay/data
.venv/bin/python -m scripts.validate_table_recovery --output artifacts/table-recovery-replay --render --probes
```

验证脚本使用既有四份真实 DXF 的路径和哈希清单，复制到独立数据目录。逐表对比恢复操作前后的原表格读取/语义结果，对沙箱实际返回的候选执行错误注入，再输出来源引用检查与原图叠加图。

现有可返回网格只作为机制对照；既有失败项不自动当作真实表格。没有新收集的独立失败正例，所以不计算新增恢复收益或识别准确率。基于历史原图检查认定的非表格候选，以及无法定位的代理实体导致的未解决状态，分别保留诊断。

最终证据目录为 `artifacts/table-recovery-stage1-20261016/final/`，包括 `summary.json`、`sandbox-probes.json`、逐图 JSON、完整 `inputs/`、原始 `executions/`、`review.html` 与 PNG/SVG。代码保存在 `frozen-implementation/`，并附输入、实现和冻结文件哈希。最终数量以该冻结批次汇总为准；上层目录保留开发与失败运行记录，不混算。

固定方法登记为 experimental；方法代码、镜像、参数和验证器版本均留存。本阶段没有自动方法晋级或永久修改公共组件的入口。

## 最终真实回放结果

证据类别：离线真实 DXF、固定方法 Docker 执行及独立来源复读；没有平台 E2E、LLM 推理或工程语义验收。

| 原始图纸 | 既有候选 | 固定方法返回网格 | 保留未解决 | 输入超限拒绝 |
| --- | ---: | ---: | ---: | ---: |
| `(6审)20231110-奥体北-北区地库节点大样图_t3.dwg` | 36 | 0 | 36 | 0 |
| `(6审)06-20#南区地下室人防平面图（三审）_t3_t3.dwg` | 3 | 0 | 3 | 0 |
| `(6审)E1楼_t3.dwg` | 6 | 0 | 6 | 0 |
| 真实设计说明样本 case59 | 34 | 25 | 1 | 8 |
| 合计 | 79 | 25 | 46 | 8 |

71 份完整冻结输入实际进入容器；8 个超限输入在执行前拒绝。前三份图纸的无法定位代理实体等诊断阻止自动给出网格，不能把这45项归类为算法已确认“不是表格”；其中原组件拒绝的9个非表格候选仍保留未解决。

case59 的25个返回网格共647个单元格，2192次结果原文引用检查通过。基于一个真实成功输出构造的12类错误全部被拒绝：跨输入、空网格、越界、虚构文字实体、丢字、重复文字、虚构格线、无支撑边、重叠格、伪造原文、串格、全表一格。这不是12张独立真实图纸或12个自然发生的算法错误。

全部79个候选的既有原文读取和网格结果在扩展前后保持相同；四份源 DXF SHA-256 不变。独立重读原始 DXF 核对71个输入包中的53,792条去重原生线段和3,343个去重文字实体，实例路径、线段端点和文字全部相符，见 `source-bindings-audit.json`。这些检查只证明来源，不证明整图完整性或表格语义。

HTTP 实测验证诊断、分页、真实固定方法执行、幂等、SVG、动态代码拒绝（422）、跨快照读取拒绝（404），见 `http-verification.json`。Docker 实测包含无网络、无挂载、非root、根目录只读、超时、输出超限与容器清理。新增专项20项通过；交付时完整工作区473项通过、4项跳过，日志为 `full-tests-delivery-workspace.log`，完整集合含同期其他功能测试。共享任务编排代码在冻结回放后仍有更新，详见 `workspace-delivery-check.json`；本次扩展模块与冻结版本一致，不将整套当前工作区误称为逐字节相同的回放版本。

原图抽查：case59候选25为“部分室内、室外工程作法表”，浏览器 SVG 的85个候选格与5列、17行原图布局对应；节点大样图候选14为楼梯区域，没有输出网格。只做局部抽查，未将所有25个网格标为人工认可。

报告最终展示使用浏览器 SVG，避免 `resvg` PNG 对大WCS坐标下 `non-scaling-stroke` 的显示差异。`presentation-update.json` 保存 HTML 展示调整前后哈希；冻结计算脚本保留原样，当前重放脚本仅将报告图片引用从PNG改为SVG，计算代码和输入输出未变。
