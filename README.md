# CAD Atlas · 通用图纸事实工作台

CAD Atlas（仓库名 `cad_opt_agent`）是面向 DWG / DXF 的本地图纸事实分析工作台。它将原生文字、表格、图元和局部视图整理成可检索的来源，由 Agent 按问题读取、计算和交付，并将回答引用定位回原图。

当前能力已从砌体事实提取扩展到设计说明、构造做法、复杂表格、业务指标、关联图纸查阅和二维几何分析。项目独立于 `cad_llms_group`，不包含优化、成本或合规策略；来源绑定、计算成功与工程对象身份确认分别处理。

> 本文按 2026-10-28 本地 `main` 代码及工作区核对。任务内动态方法、工具修订与多模块程序已在当前代码中；新增几何工作台仍有未提交实现，单独列为开发中。历史验证报告只代表各自冻结版本与样本，不代表当前全部代码已通过真实图纸验收。

## 快速开始

需要 Python 3.11+。在仓库根目录执行：

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[test]'
python -m cad_agent
```

打开 [本地工作台](http://127.0.0.1:8765)。FastAPI 同源提供原生 JavaScript ES Module 前端，无需 Node 构建；当前按源码或可编辑安装运行，不作为独立 wheel 分发。

1. 点击 **打开示例**，或通过左上角 `＋` / 拖放导入 DXF、DWG。DXF 在本地解析，DWG 需要配置 ODA 转换服务。
2. 在输入框上方选择问答模型并完成相应配置。没有模型连接时，可在设置中选择 **本地 · 事实查询**，使用本地提取和原文检索。
3. 输入具体问题，例如“列出外墙做法，保留编号、完整层次和备注”或“读取经济技术指标中的用地面积与容积率，并标明来源和单位”。
4. 点击回答的编号引用定位原图；通过 **查看运行详情** 检查模型调用、工具输入输出、校验与失败原因。

工作台采用左侧文件、中间原图、右侧问答三栏。左下角 **原文与事实** 提供检索、图纸结构、解析诊断和重新解析入口；设置中的 **关联图纸** 决定本轮允许查阅的其他快照。操作说明见 [精简工作台](docs/workspace-ui.md)。

## 模型与运行时

模型配置和 Agent 运行时是两个选择；实际使用值、用量与回退情况写入运行记录。**当前 `/api/models` 和网页初始选择为 `api_sol`，不是订阅模型**；配置存在也不等于目标模型可调用。

### ChatGPT 直接订阅

使用自定义订阅 Harness 时，在模型列表选择 **标准 · GPT-6.1 Sol · Low**（`siwc_sol`），或按需选择深度档 `siwc_sol_high`、`siwc_luna_high`。页面自动匹配 `responses_subscription`（自定义 · 订阅直连），并关闭付费 API 回退。

在 **设置 → 账户与授权** 完成本应用独立的 Sign in with ChatGPT 登录，在系统浏览器授权后刷新状态。无需 API key；凭据保存在所选数据目录下的 `siwc-subscription/`，不读取桌面 Codex 令牌。登录成功不代表模型权限或推理服务已验证。授权机制和历史实测见 [直接订阅说明](docs/siwc-subscription.md)；其中早期工具数量、预算和界面描述以当前实现及本文为准。

### API 配置

在仓库根目录创建或编辑 `.env`，可参考 [配置示例](.env.example)；已有文件不要覆盖。环境变量优先于 `.env`，修改后需重启服务。

当前网页默认的 GPT API 配置：

```dotenv
CAD_GPT_API_BASE_URL=https://your-openai-compatible-endpoint.example/v1
CAD_GPT_API_KEY=填写你的服务密钥
```

`api_sol` 使用代码中配置的 `gpt-6.1-sol`，`api_astra` 使用 `gpt-6-astra`。应填写实际兼容服务地址，并核实服务支持这些模型名称。

默认提供方及手动模型辅助提取可配置 DeepSeek：

```dotenv
CAD_AGENT_PROVIDER=deepseek
DEEPSEEK_API_KEY=填写你的DeepSeek密钥
```

该配置在代码中固定使用 `https://api.deepseek.com`、`deepseek-flash` 和 Chat Completions 协议；在问答模型列表选择“默认”才使用此配置。手动模型辅助提取使用默认提供方，与当前问答模型选择分开。密钥仅由后端读取，`.env` 已被 Git 忽略，可使用 `chmod 600 .env` 限制权限。

### 兼容运行时与检索

`native` 与 `responses_subscription` 已接入项目自有的共享执行内核，包含步骤快照、工具生命周期、只追加事件、运行中修改问题和有界纯计算。实现边界与 API 见 [CAD 自定义 Harness](docs/cad-harness.md)。

| 入口 | 当前用途 |
| --- | --- |
| `native` | 自定义 API 工具循环，API 请求的默认运行时 |
| `responses_subscription` | 自定义 ChatGPT 订阅工具循环，支持当前任务聚焦与受控动态方法 |
| `deepseek_harness` / `codex` | 可选 CLI 桥接；需要各自依赖，失败回退默认关闭 |
| `codex_subscription`、Qwen 配置 | 后端保留历史运行/API 兼容，当前网页模型列表已隐藏 |

桥接安装和兼容说明见 [运行时说明](docs/agent-runtimes.md)、[Codex 订阅说明](docs/codex-subscription.md)、[Qwen 配置](docs/qwen-models.md)。这些文档包含历史测试，不据此断言当前服务可用。

检索默认 `keyword`；可选本地 BGE embedding 与 BM25 合并的 `hybrid`。模型文件需显式准备，问答不会自动下载；依赖或模型不可用时显式回退。需要时执行：

```bash
python -m pip install -e '.[retrieval]'
python -m scripts.prepare_embedding
```

安装、存储位置及限制见 [混合检索说明](docs/hybrid-retrieval.md)。

## DWG 导入与数据保存

DXF 直接读取原生实体。DWG 由后端调用配置的 ODA 服务，转换为 DXF 并核验完整性后导入；转换、鉴权或解析失败时返回原因，不创建成功的图纸记录。

```dotenv
CAD_ODA_BASE_URL=http://your-internal-oda-host:18080
CAD_ODA_API_KEY=填写内网转换服务密钥
CAD_ODA_TIMEOUT_SECONDS=600
CAD_AGENT_DATA_DIR=.data
CAD_AGENT_PORT=8765
```

ODA 密钥仅用于后端转换。成功后可下载原 DWG、转换 DXF 和 ODA JSON；主解析流程以转换 DXF 为来源快照。详见 [DWG 导入](docs/dwg-import.md)。

默认 `.data/` 保存原文件、解析场景、SQLite 元数据、任务、运行记录和授权资料；`artifacts/` 保存检查与实验产物。两者及 `.env` 均被 Git 忽略。测试图纸按内容去重、报告共享输入，整理与迁移见 [artifacts 保存策略](docs/artifact-storage.md)。

## 当前能力

### 设计说明、构造做法与来源记录

- 检索 TEXT、MTEXT、块实例、属性文字、章节和表格来源；按范围读取上下文、构造层次、标题、备注和引用关系。
- `search_source_records`、`read_assembly`、`select_source_records` 支持做法记录的发现、完整读取和明确字面条件筛选；保留原始编号、适用范围及未确认归属。
- 默认启用 `CAD_LLM_QUERY_INTENT=1`：模型先提交绑定用户原句的查询意图，分别记录对象、编号、条件和要求，再调用工具。用户条件不是图纸证据。
- `CAD_SOURCE_RECORD_WORKFLOW=1` 默认开启来源记录能力；当前默认配置不会在 Agent 之前直接走旧确定性快捷路由。设置 `CAD_LLM_QUERY_INTENT=0` 才恢复该路由尝试，且仅符合条件的问题能直接整理原文。不要将“自动整理原文”理解为必定零模型调用。
- 回答保留原文事实、推导、未知与冲突；原文字符区间、实体和快照接受后端校验。字段证据区分原文标称值、标注、测量和计算口径。

详见 [查询意图与精确执行](docs/llm-query-intent.md)、[来源记录工作流](docs/source-record-workflow-report-20261023.md)、[文本与证据锚点](docs/text-metadata-anchoring.md)。

### 复杂表格与业务指标

表格已作为独立文档处理，不再仅限早期有边线的表格行：

- 从 ACAD_TABLE 元数据、原生线框、块实例、局部旋转和无框对齐文字发现表格或候选。
- 保留行列、合并跨度、表头路径、重复分组、内部子表、单位及备注；分别记录文字来源、结构支持和字段归属。
- 通过 `search_tables`、`inspect_table`、`read_table_content` 检索和分页阅读，持久化阅读计划、游标及已交付单元格集合。
- `recover_table_region` / `review_table_region` 提供局部恢复与视觉复核；纯视觉转录保留候选状态。
- `extract_planning_metrics` 和 `extract_building_schedule` 分别提取经济技术指标与建（构）筑物一览表，保留原始口径、设计/控制值、单位、限定、缺失和冲突。规划指标也可来自单个原生文字实体内明确声明的指标段落。

整表读完不证明整张图纸解析完整；结构、归属或来源不足时不能将候选升级为确认值。界面支持逐单元格定位及表格叠加预览。详见 [复杂表格文档](docs/complex-table-documents.md)、[业务表格提取](docs/business-table-extraction.md)、[固定方法表格恢复](docs/table-recovery-stage1.md)。

### 关联图纸、任务与计算

本轮可显式选择最多 8 张关联图纸。`read_project_sources` 提供选定资料清单、检索和原文读取；来源保留各自快照。文件名、用户填写的项目/楼层/版本标签不证明跨图对应关系，也不自动完成全项目比对。

复杂问题通过 `plan_task` 拆成分项，记录已读来源、对象、工具观测、交付缺口和恢复状态。`calculate_facts` 区分图纸数值、用户明确输入和测量结果，保留单位与来源；不据此执行优化或合规判断。单位或范围确认后可恢复对应流程，未解决项与已完成项分别展示。

详见 [任务编排](docs/task-orchestration.md)、[聚焦与交付](docs/focused-delivery.md)、[执行策略 v2](docs/cad-agent-execution-v2.md)。

### 二维几何、房间与语义组件

共享工具覆盖图框/子图上下文、原生尺寸、轴网、索引引线、标高、表格语义、源几何查询、相似图形候选和局部视图。具备房间候选发现、闭合轮廓检查、指定房间测量、楼层名称候选批量测量，以及绑定对象后的面积、截面、距离、包含/重叠、门扇旋转候选关系分析。

局部视图可用于支持视觉工具的运行时；工具可用性由 `/api/tool-capabilities` 按运行时和提供方返回。视觉识别和相似形状均不能独立确认对象身份。几何结果保留源路径、范围、单位、计算口径、近似精度和未解决条件；未知单位不会自动猜成毫米，局部截面宽度不代表全路径最小净宽。

以下完整短句可直接发起浏览器本地交互：

| 输入 | 操作 |
| --- | --- |
| 测量房间 / 测量选中房间 | 室内选点或使用已定位房间名称，进入测量与单位确认 |
| 识别当前视野的房间 | 在当前视野发现房间候选 |
| 框选找相似 | 框选样本并查找图形候选 |
| 查看测量记录 | 查看已有测量、边界与局部图 |
| 下载原图 / 下载图纸 | 返回当前快照可下载文件 |
| 局部方法调试 | 打开范围和方法确认窗口 |

这些入口标为“本地操作”；带有其他分析要求的自然语言问题继续进入 Agent。详见 [公共组件](docs/drawing-components.md)、[语义组件](docs/semantic-components.md)、[房间几何试点](docs/room-geometry-pilot.md)、[房间轮廓](docs/room-contours.md)、[相似图形检索](docs/similarity-search.md)。

### 任务内动态方法与程序（实验功能）

默认 `dynamic_methods=off`。在高级选项勾选 **允许动态方法**，或在本轮问题中明确写“允许使用动态代码”，才开放受控路径；明确禁止优先。当前仅支持 GPT-6.1 Sol + `responses_subscription`，需要预先准备 Docker 沙箱，不启用付费回退。

| 能力 | 当前实现边界 |
| --- | --- |
| `develop_method` / `run_task_method` | 对真实工具观测绑定的局部输入生成表格、文字或几何方法，独立验证后读取结果 |
| `inspect_tool_implementation` / `revise_tool` / `execute_tool_version` | 修订 `read_assembly` 的片段分组/排序内核；保存真实 diff、运行原版与新版回归，按任务和目标路由 |
| `develop_program` / `execute_program` / `read_program_result` | 至多四个 Python 模块组成文本分析程序；契约支持筛选、字段提取、归一、数值解析、计数、求和、比较、连接、排序和去重 |
| `register_program` / `list_programs` | 至少两组不同图纸来源验证通过后登记为 experimental；跨任务复用代码，新输入重新执行与验证 |

生成代码在固定镜像、无网络、无宿主挂载、只读文件系统、非 root 容器中执行，不动态安装依赖。宿主重新检查来源、契约、中间结果、数值和引用；失败、取消、预算耗尽保留记录，不自动发布公共工具。通过验证不等于工程语义已确认。

需要使用实验功能时，先准备 Docker 和基础镜像，再为实际数据目录构建隔离运行环境：

```bash
docker pull python:3.11-slim
python -m scripts.setup_table_recovery_sandbox --data-dir .data
python -m scripts.setup_adaptation_sandbox --data-dir .data
```

普通导入、阅读与问答不需要 Docker。详细授权、执行和证据边界见 [任务内动态方法](docs/task-verified-methods.md)、[工具修订与多模块程序](docs/task-tool-revisions-programs.md)。

### 几何工作台（工作区开发中）

当前工作区新增局部原生几何包、直线/圆弧成对测量、候选对发现、几何假设登记、路线采样、叠加预览及结果导出，并接入几何上下文投影与交付检查。相关代码位于 `geometry/workbench*.py`、`geometry/local_measurement.py`、`geometry/candidate_routes.py` 和 `analysis/geometry_context.py`，尚有未提交改动。

`CAD_GEOMETRY_VALIDATION_MODE` 默认 `shadow`，显式设为 `enforce` 才启用相应强制校验；`CAD_GEOMETRY_CONTEXT_PROJECTION` 默认 `1`。模型声明的对象身份、选定障碍和采样路线仍不代表已确认工程对象、障碍枚举完整或全路径最小值。方案与验收计划见 [几何 Agent 改进计划](docs/general-geometry-agent-improvement-plan-20261028.md)，不将计划或代码存在视为真实端到端通过。

## 执行预算、记录与接口

默认 `agent_policy=efficient`、`CAD_EXECUTION_POLICY=v2`。API 基础限制为 8 轮、16 次工具调用、180 秒；直接订阅使用单独的自适应预算，并按任务类型调整，不能套用 API 基础限制。具体预算由 `runtimes/budget_policy.py` 定义，见 [运行时预算](docs/runtime-budgets.md)。

`source_read_batching` 默认关闭，仅显式设为 `true` 或勾选界面实验选项才开启。来源读取、模型调用、动态程序和最终交付仍受预算限制；预算不足保留已读结果和未完成状态。完整工具结果可以存档并分页回读，存档或摘要不自动获得事实证据资格。

常用接口（服务启动后）：

| 接口 | 用途 |
| --- | --- |
| `GET /api/health`、`/api/models`、`/api/runtimes` | 配置与可用入口；不是实际推理成功证明 |
| `GET /api/tools`、`/api/tool-capabilities` | 工具目录和运行时能力 |
| `POST /api/drawings` | 导入图纸 |
| `POST /api/chat-runs`、`GET /api/chat-runs/{run_id}` | 提交和读取异步问答 |
| `POST /api/chat-runs/{run_id}/cancel` | 取消分析 |
| `GET /api/drawings/{snapshot_id}/tasks`、`/api/tasks/{task_id}` | 任务与恢复状态 |
| `GET /api/tasks/{task_id}/evidence-records` | 完整证据档案目录与分页回读 |

API 直接提交模型问答时应显式指定 `mode=agent`；`ChatRequest` 的默认值是本地查询。以下订阅请求中的快照 ID 必须替换为导入返回值：

```json
{
  "snapshot_id": "替换为实际snapshot_id",
  "question": "列出外墙做法，保留编号、完整层次和备注，并给出来源。",
  "mode": "agent",
  "model_profile": "siwc_sol",
  "runtime": "responses_subscription",
  "allow_fallback": false,
  "retrieval_mode": "keyword",
  "source_read_batching": false,
  "dynamic_methods": "off"
}
```

完整接口以 [本地 OpenAPI](http://127.0.0.1:8765/openapi.json) 为准。运行详情支持步骤筛选、原文定位、校验与修复回看、历史恢复及记录导出，见 [运行详情说明](docs/run-inspector.md)。

## 开发与验证

```bash
python -m pytest -q
node --check web/app.js
node --test tests/*.mjs
```

Python 回归和 Node 前端测试不等同于真实模型验收。可按功能单独运行 `tests/test_task_methods.py`、`tests/test_task_programs.py` 等；设置 `CAD_TEST_TASK_SANDBOX=1` 可启用相应真实 Docker 测试，需要已准备沙箱。真实图纸/模型验证应使用对应脚本，明确样本、模型、数据目录和执行开关，并保存冻结版本及失败记录；不要把带模型调用的检查当成纯本地测试。

| 目录 | 职责 |
| --- | --- |
| `cad_agent/cad/` | 原生读取、文字/表格索引、来源与渲染 |
| `cad_agent/analysis/` | 查询意图、任务、来源记录、工具、证据及交付校验 |
| `cad_agent/tables/` | 表格文档、结构恢复、分页读取与来源验证 |
| `cad_agent/components/`、`semantics/`、`geometry/` | 图纸上下文、标注语义与二维几何 |
| `cad_agent/runtimes/`、`adapters/` | 模型协议、运行时、订阅、ODA 与存储适配 |
| `cad_agent/extensions/` | 固定表格恢复、隔离动态方法、工具内核与程序契约 |
| `web/` | 三栏工作台、原图渲染、引用与运行记录界面 |
| `tests/`、`scripts/`、`docs/` | 回归、受控验证脚本与专题说明 |

历史证据入口包括 [验收记录](docs/acceptance.md)、[复杂表格验证](docs/complex-table-validation-20261026.md)、[查询意图验证](docs/llm-query-intent-validation-20261026.md) 和各功能文档。合成回归、离线重放、真实模型调用、视觉核对与工程验收分别记录。

## 使用边界

- 当前是单机、单用户、单进程工作台，只监听环回地址并校验 Host/Origin；没有多租户鉴权或生产集群部署。
- 默认上传上限 32 MB。实体展开、表格发现、几何处理、分页和模型上下文均有预算；诊断与已读覆盖不能扩张为全图完整性承诺。
- 字体、代理对象、外部参照、非平面表格和复杂曲线可能存在解析或显示限制。复杂表格支持不意味着任意合并、跨页继承或扫描表格都能自动准确恢复；未提供通用 OCR 服务。
- 原图及局部预览使用轻量绘制，适合来源复核，不替代精确 CAD 出图；详见 [显示绘制](docs/cad-rendering.md)。
- 房间名称是文字锚点，图框是范围候选，相似图形是匹配候选；不能直接作为确认对象数量、净面积或工程身份。
- `source_bound` 表示来源绑定，不表示工程正确；单元格覆盖、几何闭合、动态程序验证也各自只有明确范围。
- 模型问答会按工具需要发送选定图纸的相关文字、元数据及局部图像。订阅登录、服务配置存在或接口返回成功都不能替代实际工具调用与证据校验。
- 不自动作优化、合规、成本或工程适用性结论，不以历史个案宣称整图召回率或通用正确率。
