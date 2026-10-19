# 受控动态方法适配

这是独立扩展入口。既有 `read_table`、语义读取、房间和公共组件调用不会自动改走 LLM，也不覆盖原结果。表格固定扩展的验证器升级为 `table-recovery-validator@2`，会拒绝原来漏检的原生分隔线。

## 可运行流程

1. 用户选择已有表格，或为非表格方法明确指定布局与局部 bbox。
2. 宿主从当前快照构造完整、有大小限制的输入，记录 DXF 和三个解析产物的 SHA-256。输入不完整、超限、已识别为图形的表格直接拒绝。
3. `auto` 模式先运行已审查的全宽行锚点补充方法；未通过时再调用配置中的模型。`fixed` 永不调用模型；`dynamic` 用于对照实验；`replay` 重放已验证候选的同一代码；`registered` 运行指定已注册版本。
4. 模型输出 `AdaptationProposal`。代码、提案、输入、执行结果、验证报告各自按内容摘要存储，不使用相同数据库 ID 混放不同类型。
5. 固定镜像中的 `adapt(bundle)` 执行一次。代码可用标准库及 `sdk.recover`，不能选择镜像、安装依赖或提供宿主路径。
6. 宿主重新校验来源，独立检查输出。失败原因作为结构化反馈，最多三次模型生成，最多四次执行（含一次固定方法）。默认总预算 180 秒、每次执行 20 秒、保守 token 预留上限 96,000；每个输入范围和方法最多六个任务，幂等重试不重计。
7. 通过结构验证的候选进入 `awaiting_review`。原图、候选叠加、结果及审计记录可读取。人工复核绑定候选 SHA 与明确范围，不等价于工程语义验收。
8. 通过的代码自动登记为 `experimental`。相同代码在至少两张不同来源图纸上获得相同范围的人工确认后，才可显式注册。注册记录保留验证器版本；禁用会阻止后续运行并留下事件。不同图纸不自动等于不同项目，记录始终不宣称跨项目泛化。

## 已接入的五类方法

| method | 输入 | 独立验证范围 | 不会自动证明 |
|---|---|---|---|
| `table_grid` | 现有 table_id；原生线段、原文、锚点 | 原图边界、原生坐标锚点、完整分隔线、网格覆盖、文字归属、不重不漏 | 非表格身份、表头含义、工程规则 |
| `text_spans` | 表格范围或显式局部范围 | 原生 entity_id、连续原文区间、来源证据 | 选择语义正确或已经找全 |
| `segment_measurements` | 表格范围或显式局部范围 | 来源线段、独立长度重算、重复/伪造拒绝 | 单位换算、构件身份、数量统计 |
| `segment_distances` | 表格范围或显式局部范围 | 两条原生有限线段的最短距离重算、交叉/重叠、重复及伪造拒绝 | 墙厚、门宽、间隙语义和单位换算 |
| `closed_contour` | 表格范围或显式局部范围 | 原生边组成单个简单闭合轮廓、无多余边、范围及面积重算 | 房间身份、墙内/外边界、净面积扣除 |

Agent 旁路现支持上述五类方法；按方法和局部区域配置，见 [多方法旁路](adaptation-shadow.md)。

其他方法名不开放任意执行。新增方法需要注册自己的契约、可信输入投影、验证器和测试；工程判断、标注关联等尚不能用上述几何验证替代。原生非直线几何保留为 exclusions，当前执行 SDK 不支持曲线重建。

## HTTP 接口

前缀：`/api/drawings/{snapshot_id}/adaptations`。

| 接口 | 用途 |
|---|---|
| `GET /methods` | 方法契约与实验/注册版本 |
| `POST /runs` | 创建异步任务，返回 202 与 run_id |
| `GET /runs/{run_id}` | 状态、预算、提案/代码/执行/验证引用、模型 token 用量 |
| `POST /runs/{run_id}/cancel` | 取消；容器立即终止，正在等待的模型 HTTP 调用最多等待其 55 秒超时 |
| `GET /runs/{run_id}/result` | 已验证候选及来源信息 |
| `GET /runs/{run_id}/review` | 人工复核界面 |
| `GET /runs/{run_id}/source.svg`、`overlay.svg` | 原生根对象及依赖块渲染，与候选叠加 |
| `POST /runs/{run_id}/reviews` | 人工签认或拒绝，要求 candidate_sha256 与范围匹配 |
| `POST /versions` | 同代码跨图纸人工验收后显式注册 |
| `POST /versions/{version_id}/disable` | 禁用与审计事件 |

创建请求示例：

```json
{"table_id":"table_...","method":"table_grid","instruction":"恢复此区域网格","mode":"auto","model_profile":"default","idempotency_key":"unique-request"}
```

非表格范围示例（不能同时提供 table_id）：

```json
{"method":"segment_measurements","layout_id":"layout_...","bbox":[0,0,120,45],"instruction":"计算范围内原生线段长度","mode":"dynamic","idempotency_key":"unique-region"}
```

重放同一代码使用 `mode: "replay"` 和 `reuse_run_id`；已注册方法使用 `mode: "registered"` 和 `method_version_id`。两者都不重新调用模型。

复核请求必须由用户提交：

```json
{"candidate_sha256":"...","decision":"accepted","scope":"table_identity_and_grid","note":"原图确认此区域分格正确"}
```

注册请求：`{"name":"方法名称","run_ids":["两个不同图纸的已签认运行ID"]}`。只注册该代码及其明确范围；不会修改公共组件或把试验数据写成正式基线。

## 隔离与来源边界

动态执行沿用 Docker Desktop Linux 隔离：无网络、无宿主挂载、只读根文件系统、非 root、全部 capabilities 丢弃、禁止提权、进程数 32、内存 512 MiB（无额外 swap）、CPU 限额、64 MiB 临时目录、输出 2 MiB、stderr 64 KiB。宿主 watchdog 处理超时、输出超限和取消，并删除容器及子进程。没有宿主 Python 回退路径。

AST 仅用于语法和入口检查，不作为安全边界。容器隔离不宣称能抵御内核漏洞；部署侧需维护 Docker/宿主安全更新。模型不接触宿主凭据。外发内容是具有显式格式的局部文本/原生几何投影，模型调用记录提供商、模型、提示版本/摘要和 token 用量。价格未配置时不虚构人民币成本。

原始输入不可由模型改写。表格候选新增锚点由宿主核验原生线段支持，重新构造并封存派生输入；边界必须完全相等，覆盖诊断、原文与原线段保持不变。验证器扫描全部原生坐标，不能通过遗漏输入轴线掩盖真实分隔线。

复核用原图根对象及其依赖块生成局部上下文，防止整图预览遗漏；这一子集不会成为算法输入，来源哈希、根句柄、渲染诊断另存。未能渲染不等于没有内容。原图身份和渲染完整性仍由人工复核，不能用候选网格作为自己的验收答案。

## 运行和验证

在仓库根目录运行：

```sh
.venv/bin/python -m scripts.setup_adaptation_sandbox --data-dir .data
.venv/bin/python -m scripts.probe_adaptation_sandbox
.venv/bin/python -m pytest tests/test_adaptation.py tests/test_table_recovery.py
```

镜像构建重用已审计 `table-recovery-runtime.json` 中固定摘要的基础镜像。应用运行时只使用固定镜像 ID，不拉取或构建。

`scripts.validate_adaptation` 在独立目录做真实样本对照，支持 `--scan-only`、`--mode fixed` 和明确的 `--source/--indices`。`--families` 验证原文/长度/轮廓三个适配器。真实模型运行必须遵守当前任务已授权的数据外发范围和预算。

本次运行证据位于 `artifacts/dynamic-adaptation-20261016/`。既往人工签认保留在原审计目录；新代码、新输入或新候选不会自动继承旧签认。
