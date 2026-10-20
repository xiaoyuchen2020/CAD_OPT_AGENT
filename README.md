# CAD Atlas · 通用图纸事实工作台

当前里程碑：原生 DXF → 通用砌体事实与原文索引 → DeepSeek 工具问答 → 原图证据定位。独立于现有生产仓库，不包含 AR024 策略、优化判断或一次性代码执行。无密钥时可选择本地事实查询。

测试图纸按内容去重保存，运行记录引用共享输入；整理命令、保留范围及搬迁方式见 [artifacts 保存策略](docs/artifact-storage.md)。

## 启动

Python 3.11+，在本仓库目录执行：

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[test]'
python -m cad_agent
```

打开 <http://127.0.0.1:8765>。前端为原生 ES Module，由 FastAPI 同源提供，无需 Node 构建。当前交付采用源码/可编辑安装方式，不是独立 wheel 分发。

点击 **打开示例**，得到 9 条合成记录（包含 2 条表格记录），其中 2 条信息不完整。点击事实或问答中的引用，定位原始实体范围；通过 **原文检索** 可直接检索未生成事实的区域。支持导入 DXF / DWG（DWG 自动经内网转换为 DXF）、搜索/筛选、图框范围选择、原图缩放平移、下载原文件以及 JSON 导出。

界面的文件、原图、问答三栏布局参考 [Nomic 文档与产品工作空间](https://docs.nomic.ai/)，未复制其品牌和远程图片资源。

## DWG 内网转换

导入图纸时选择 `.dwg`，后端自动调用内网 ODA API，等待转换、核对来源与 DXF 完整性后进入现有 DXF 读取流程。转换失败、超时、授权失败或 DXF 无法读取时，页面显示原因并终止本次导入，不创建图纸记录或启动提取。原有 DXF 导入直接在本地处理。

在 `.env` 配置 `CAD_ODA_BASE_URL=http://192.168.1.146:18080`、`CAD_ODA_API_KEY` 和可选的 `CAD_ODA_TIMEOUT_SECONDS=600`，然后重启本地服务。访问密钥仅保存在后端；未配置时 DXF 导入仍可使用，DWG 导入会明确提示缺少配置。当前仅用于公司内网评估。

成功导入 DWG 后，可分别下载原始 DWG、转换 DXF 和 ODA 原生 JSON；模型和现有解析器仍读取 DXF，不用不完整 JSON 几何替代。接口、来源标识和故障说明见 [DWG 导入说明](docs/dwg-import.md)。

## 模型配置

编辑本仓库根目录 `.env`（已有文件请直接编辑，不要覆盖）：

```dotenv
CAD_AGENT_PROVIDER=deepseek
DEEPSEEK_API_KEY=在这里填写你自己的DeepSeek密钥
```

`.env` 的完整路径为 `/Users/vigorchen/Documents/WorkExplore/cad_opt_agent/.env`，已被 Git 忽略，建议权限 `chmod 600 .env`。密钥仅由后端读取，不输入网页、不放入源代码。环境变量优先于文件；填写后重启本地服务。

DeepSeek 模式固定使用 `https://api.deepseek.com`、`deepseek-flash`（DeepSeek-V4.1-Flash）、Chat Completions 协议。旧 `CAD_AGENT_API_KEY` 不会作为 DeepSeek 密钥回退，避免误用旧网关凭据。`configured` 只表示配置存在，不证明服务可用。

右侧选择 **模型 · 工具问答**：模型按问题调用图纸概览、原文检索、上下文读取、本地砌体抽取、事实查询、证据读取；只发送当前图纸相关文字与必要元数据，不上传完整 DXF。已有图框筛选作用于事实列表/手动抽取，模型问答范围是当前整张图纸，问题中可进一步指定范围。

输入框上方可直接切换模型。点击 **设置** 调整问答方式和检索方式；运行时与失败回退收在 **高级选项**，订阅登录入口收在 **账户与授权**。切换模型不会自动展开授权面板。输入框下方展开 **图纸提取**，可选择提取方式并重新提取。

回答分为原文事实、模型推断、无法确定和冲突；引用由后端校验、绑定坐标，点击引用可定位原图。工具失败或模型失败会明确显示，不冒充本地查询成功。可手动切换 **本地 · 事实查询**。模型答案单独保存，不覆盖已有事实。界面支持同图纸最近三轮追问上下文。

手动“模型辅助提取”保留此前来源片段复核能力，与工具问答分开。模型不能执行代码、修改文件、安装依赖或接入业务策略。

接口依据 [DeepSeek 模型发布说明](https://api-docs.deepseek.com/news/news260910/)和 [Tool Calls](https://api-docs.deepseek.com/guides/tool_calls/)。MVP 显式关闭 thinking，以非思考工具调用模式运行；默认最多 8 轮交互（最后一轮保留给答案整理）及 1 次最终答案纠正、16 次工具调用、180 秒预算，单次网络调用最长 60 秒。

先做合成图工具调用检查，再显式执行真实样本检查（后者会向 DeepSeek 发送样本相关条款）：

```bash
python -m scripts.check_agent --synthetic
python -m scripts.check_agent --manifest artifacts/real-validation-20261014/samples.json
```

本地报告位于 `artifacts/agent-validation-20261014/`，验证限制见 [模型问答验收](docs/agent-validation.md)。

## 验证

```bash
python -m pytest -q
node --check web/app.js
```

自动化验收、浏览器检查与真实样本限制见 [验收记录](docs/acceptance.md)。接口与数据契约见 [架构说明](docs/architecture.md)，机器可读接口定义可直接读取 <http://127.0.0.1:8765/openapi.json>。工具清单位于 `/api/tools`。

## 当前边界

- 支持原生 DXF 的 TEXT、MTEXT、块实例和属性文字，以及有明确边线的表格行。DWG 自动经配置的内网 ODA 服务转换为 DXF，来源定位绑定该转换快照；失败会提示并终止导入。
- 图框为封闭边框与标题的候选推定，无法唯一归属时回退到布局范围。字体可能替代，文字定位是实体/区域估算框，MTEXT 不虚构逐条款精确框；证据保留原文字符范围。
- 材料提及、规范引用、禁用条款与明确做法区分记录；缺失值不推断。B05、地下、特殊部位不会被优化策略提前过滤。`source_bound` 只表示来源绑定，不表示工程正确或人工验收。
- 覆盖只针对当前解析出的来源区域，不证明整图语义完整。复杂合并单元格、跨页继承、扫描件 OCR、代理对象完整语义和跨图纸对比尚未实现。主图及局部预览采用轻量绘制：保留文字布局、可见性、标注与实心填充；使用系统文字、实线、花纹边界，并简化密集折线，适合查看主要内容而非精确出图，详见 [DXF 显示绘制](docs/cad-rendering.md)。
- 默认最多 32 MB、80,000 个展开图元；复杂曲线预览最多 20,000 个细分点。手动模型辅助提取每批约 18,000 字符，最多 12 批；单个来源区域超限会明确失败而不静默截断。
- 单机单用户、单进程运行。只监听环回地址，校验 Host/Origin，默认不开放网络服务；未实现多租户鉴权、队列集群与生产部署。
- `.data/` 保存原文件、场景和 SQLite 元数据；`artifacts/` 保存本次检查产物。这两个目录和 `.env` 均被 Git 忽略。

后续围绕真实问答任务补充工具和独立标注。当前不对整图召回率或工程适用性做未经独立验收的承诺。

真实样本验证与修正前后对比见 [验证记录](docs/real-sample-validation.md)。本地完整报告在 `artifacts/real-validation-20261014/comparison.html`，包含客户来源信息，不纳入 Git。

## 可替换运行时试验

网页可选择原生、DeepSeek Harness 或 Codex Harness，默认原生。两种 Harness 均通过受限本地桥接调用所选模型，共用 CAD 工具与统一证据校验，不开放代码执行。显式勾选才会失败回退，答案标明实际运行时。安装、接口与真实 DXF 对照见 [运行时说明](docs/agent-runtimes.md)。

四份真实 DXF、36 次实际模型问答的结果及语义复读发现见 [运行时对照结果](docs/runtime-comparison.md)。此次发布成功数为原生 12/12、DeepSeek Harness 10/12、Codex 11/12；来源与关键词检查不等于语义正确率，继续保留原生为默认。

## Qwen 模型选择

问答界面可独立选择 DeepSeek 或 Qwen 大、中、小服务配置，再选择运行时。实际使用模型与运行时会写入结果。配置导入、接口与同一代码快照下的模型对照见 [Qwen 接入说明](docs/qwen-models.md)。中、小服务当前拒绝自动工具调用，选择后会明确报错；不会静默换成其他模型。

## 通用图纸工具

原通用阅读层提供九个工具（本次另增五个几何工具）：原六个工具已扩展结构导航、多词检索与通用事实，新增表格读取、图元查询、局部查看。页面“浏览结构”可查看章节和表格，单元格可定位原图并打开局部 SVG。保留砌体事实原流程；通用候选事实单独存储。局部预览供人查看，尚未接入模型视觉识图。接口、证据边界和真实样本记录见 [通用工具 MVP](docs/general-tools-mvp.md)。

四模型 × 三运行时的 144 次实测结果见 [模型与 Harness 对照](docs/qwen-model-comparison.md)，包含服务端工具调用限制、失败用量与引用语义问题。

## 可选混合检索

问答可选择原关键词检索或“混合检索 · 语义增强”。后者使用本地中文 BGE embedding，与结构分段后的 BM25 排序合并；仍通过原文上下文和已有证据链回答。默认保持关键词，模型或依赖不可用时显式回退。无需额外 embedding API key。

安装与接口见 [混合检索说明](docs/hybrid-retrieval.md)，四份真实 DXF 的固定问题、Token、耗时及服务失败记录见 [对比结果](docs/hybrid-retrieval-results.md)。本次证实检索召回提升，尚未证实端到端更省 Token。

## Agent 探索控制

默认启用 `agent_policy=efficient`：复用重复调用、精简模型侧上下文，并在连续无新增信息或探索预算耗尽时整理答案。原图证据和引用校验保留；API 请求设置 `agent_policy=legacy` 可恢复旧控制逻辑。实现、4 题限额验证和 HTTP 503 导致的验证限制见 [探索优化记录](docs/agent-efficiency.md)。

## Codex 订阅本机试验

新增独立的 Codex 订阅入口，可选择 GPT-6.1 Sol / GPT-6 Astra。请在页面完成官方 ChatGPT 登录；浏览器流程失败时可使用官方设备码登录。此入口消耗账户订阅额度，不自动回退到付费 API，也不读取桌面端令牌。登录不代表目标模型权限已经验证。配置隔离、用量与运行限制见 [订阅接入说明](docs/codex-subscription.md)。

另提供独立的 **ChatGPT 直接订阅** 试验入口：通过 Sign in with ChatGPT 为 CAD Atlas 授权，自定义运行时直接调用 Responses API 并控制 CAD 工具循环。无需 API key，不复用 Codex 凭据，保留原 Codex 入口作对照。授权、接口与验证状态见 [直接订阅说明](docs/siwc-subscription.md)。

## 几何底座与房间净面积试点

新增图框／子图上下文、原生尺寸读取和墙—门洞—房间候选关系三个公共组件。通过“浏览结构”使用，或调用 `get_drawing_context`、`read_dimensions`、`query_building_relations`；结果可分页、原图定位并追溯来源。接口、真实图纸验证及限制见 [公共组件说明](docs/drawing-components.md)。

新增 `read_geometry`、`query_spatial`、`measure_geometry`、`measure_room`、`get_measurement_evidence`，所有运行时共享。使用独立计算几何和空间索引，支持图元距离/长度/闭合面积，以及指定房间的成对内墙面候选、门洞闭合、净面积和来源证据。无需模型也可操作：点击“测量房间”后在室内选点，或点击原文房间名称后的测量按钮。“测量记录”可回看边界、局部图和 JSON。

未知单位需明确选择并记录本次测量单位；不自动猜测。净边界试点仅覆盖二维直线墙面；缺墙、无外侧墙面支持、用途不明的内部闭合图形、未知曲线和不完整解析均保留待复核。计算结果不确认建筑对象身份，也不执行面积优化阈值或规范判断。代码不导入、调用或修改 cad_llms_group。

接口、计算口径、验证和使用限制见 [房间几何试点](docs/room-geometry-pilot.md)。本地生成可复核示例：

```sh
python -m scripts.check_room_geometry
python -m pytest -q tests/test_room_geometry.py
```

可选 `--live` 仅向已配置模型发送生成的合成图相关数据，用已选房间上下文验证工具问答；不能与真实 `--dxf` 同时使用。

## 轴网、索引引线、标高楼层与表格语义

新增共享工具 `query_annotations`、`locate_by_axes`、`trace_detail`、`read_table_semantics`，复用现有实体身份、空间索引、表格候选和原文证据。网页从“浏览结构”进入轴网/索引/标高列表，从表格原文进入表头、单位和合并关系视图。对象与关系保留候选或未解决状态。功能边界及冻结真实图纸验证见 [公共语义组件](docs/semantic-components.md)。

## 表格恢复固定方法试点

新增独立的表格恢复诊断、冻结输入、固定方法 Docker 隔离执行、独立验证和原图叠加接口。现有表格组件及 Agent 工具保持原行为，不接入 LLM 动态生成代码；输出保留候选与待复核状态。环境准备、接口和真实样本证据见 [第一阶段说明](docs/table-recovery-stage1.md)。

## 楼层房间批量流程

`analyze_room_batch` 按平面图标题或图框及任意简短房间名称枚举候选，复用 `measure_room` 逐项测量。无特定项目、楼层或房间类型白名单。能够从图纸原文确定明确标题和名称的单一枚举面积问题先执行通用批量流程，再由当前 Harness 汇总；混合任务保留原工具循环。工具记录中的 `initiator=deterministic_room_workflow` 区别于模型主动选择工具。

- 楼层范围要求独立图框和唯一对应标题；同名图框、多个平面标题、剖面或 fallback 范围等待明确选择。
- 短名称匹配支持可选数字编号，排除长条款和表格行。候选是文字锚点，不是确认房间总数，不能累计作总面积。未命名空间仍由 `discover_rooms` 处理。
- 每页最多 12 项，逐项保留原文证据、状态、测量 ID 和高亮类型；批次以 20 秒软预算限制继续启动测量，单项沿用已有几何范围限制。分页与未测项明确显示。
- 单位只来自 DXF 或界面明确确认；批量模型工具没有 `unit_override` 参数。未知单位时保留全部名称候选并高亮文字框，只暂停相关测量，复杂任务的独立查阅可继续；不会以框面积或默认毫米生成答案。
- 界面明确确认单位后仅运行本地批量测量，不再调用模型、不修改图纸原始单位。新批次引用旧批次并保存确认记录。候选边界与名称定位框区别显示。

HTTP：`POST /api/drawings/{sid}/rooms/batch` 使用 `floor_title` / `frame_id`、`layout_id`、`room_label`、`offset`、`limit`；`GET /api/drawings/{sid}/room-batches/{bid}` 读取完整证据；`POST .../{bid}/confirm-unit` 必须提供 `{"unit":"mm","confirmed":true}`；`POST .../{bid}/page` 使用 `{"offset":12}`，沿用该批次已确认的单位。

2026-10-15 单次真实验证：18#楼1229_t3.dwg 的 DXF 快照，“一层平面图中有哪些卧室？卧室房间面积分别是多少？并帮我高亮出来。”使用 GPT-6.1 Sol + 自定义订阅 Harness。找到 6 处卧室名称候选和 1 处楼层标题；7 处证据的文字、实体、范围与坐标均核对通过。用户明确暂不确定单位，故此次验证的是等待单位确认并完整交付候选/高亮的流程，不是真实房间面积验收。1 次注册工具执行、1 次模型请求，4,785 Token，15.713 秒，无重试、无付费 API 回退。详情：`artifacts/room-batch-20261015/comparison.json`；合成双墙面房间单位确认后的 24 m² 候选测量由本地测试覆盖。

## 可恢复的复杂任务

新增任务/子任务状态、`plan_task`、按分项记录工具执行、逐项交付检查，以及单位确认后本地恢复和显式继续综合分析。批量工具不再触发无条件全局停止。打开图纸可恢复已保存任务。接口与限制见 [任务编排说明](docs/task-orchestration.md)。

## 按任务聚焦与主动交付

自定义 Harness 按活动分项提供工具定义；原文读取覆盖后进入交付检查，模型可提交经过引用/数值/逐项检查的答案，或说明具体缺口并补充工具。通过检查立即返回，不额外请求总结。文本复杂任务保留原文与条款关系，缩减旧工具历史；视觉/多轮历史暂保留完整上下文。详见 [聚焦与交付说明](docs/focused-delivery.md)。
