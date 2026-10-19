# 动态方法第一阶段与第二阶段旁路

本实现只开放受控试运行和 Agent 旁路。默认不触发；结果保持 `shadow_only`，不会改变原组件、加入问答证据、自动采用或注册公共方法。策略接口覆盖 `table_grid`、`text_spans`、`segment_measurements`、`segment_distances`、`closed_contour` 五类方法。非表格方法可以使用明确的布局和局部 bbox，不再要求 table_id。

## 第一阶段：由宿主显式配置、显式试运行

前缀 `/api/drawings/{snapshot_id}/adaptations`：

- `POST /policies`：创建不可变策略。绑定精确的 snapshot/table 白名单、来源 SHA、模型配置/实际端点/提供方、有效期、总运行数和模型预算。
- `GET /policies/{policy_id}`：读取并核验策略封印。
- `POST /policies/{policy_id}/trials`，正文 `{"table_id":"..."}`：执行白名单内的一次旁路任务，返回运行 ID。候选通过现有 `/runs/{run_id}/result` 和原图对照页读取。
- `POST /policies/{policy_id}/disable`：阻止后续触发。已启动的任务使用原有 `/runs/{run_id}/cancel` 取消。

策略请求示意（以实际 snapshot/table/诊断 run ID 替换）：

```json
{
  "idempotency_key": "near-axis-trial-unique-key",
  "targets": [{"snapshot_id":"snap_...", "table_id":"table_...", "diagnostic_run_id":"adrun_..."}],
  "model_profile":"siwc_sol",
  "mode":"dynamic",
  "instruction":"根据原生线段及容差修正近轴线识别，保留来源、边界和原有轴线；有歧义时拒绝。",
  "max_runs":1,
  "max_model_calls":3,
  "max_reserved_tokens":96000,
  "lifetime_seconds":14400,
  "agent_shadow_enabled":false
}
```

新策略默认 `siwc_sol`，复用产品的独立 ChatGPT 直接订阅连接，模型为 `gpt-6.1-sol`；此次没有 DeepSeek 请求。订阅异常不会切换提供方、模型或付费 API。连接凭据只由现有账户适配器使用，不进入提示、沙箱或新审计目录。Responses 使用 instructions、用户 input 数组、`store=false`、`stream=true`，无模型工具；只接受完整、模型一致的成功响应。遵循[官方直接订阅约束](https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations)。

`diagnostic_run_id` 的代码及验证错误在宿主复核同输入、方法、不可变工件和候选哈希后才用于修正提示。对外数据仍需在用户授权范围内；本接口不是自动对任意新图纸外发的授权机制。局部图纸文字始终视为数据。

每个 dynamic 运行最多 3 次模型生成/验证修正，总预留 96,000 token，180 秒总运行窗口；单模型调用剩余时间不超过 55 秒，单沙箱执行不超过 20 秒。策略先保守预留每运行的 3 次/96,000 token，不返还未使用预留，以免并发超发。该预算是本地输入及输出预留控制，不是服务端硬输出 token 限额，实际 usage 单独记录。输入准备本身不能强制中断；准备后会检查总时间，超时不进入后续步骤。

重放策略使用 `mode=replay` 和 `reuse_run_id`，冻结已通过结构验证的代码，模型预算为零。相同策略、snapshot、table 重复触发复用同一任务。源变化、策略篡改、过期、禁用、预算耗尽、输入不完整和不支持情况都拒绝继续。原有每输入最多 6 个运行的上限仍适用。

## 第二阶段：Agent 在读取后触发旁路

宿主创建 `agent_shadow_enabled=true` 的策略，并在原有 ChatRequest 中显式附加 `adaptation_policy_id`。不设置该字段时保持原行为。LLM 工具列表没有新增生成、批准或注册方法工具。

`read_table` / `read_table_semantics` 的普通响应先完成，再向后台提交旁路检查。宿主只在原组件确认为 unresolved，或同输入的历史候选被独立验证器拒绝时触发；输入不完整、图形占主导、分析超限、没有宿主确认缺口时跳过。不能只凭 LLM 自报“失败”启动生成。

旁路状态写入 `adaptation_shadow_observation`、`adaptation_shadow_event`，单独关联 run ID。不会把候选、旁路说明或新证据混入当前工具响应；失败不把正常表格读取替换为错误。策略关闭不更改历史记录。当前本机单进程锁与共享执行器承担并发控制，不宣称是多租户分布式调度器。

## 隔离、验证、人工证据和沉淀

生成代码仅在已有固定镜像 SHA 的 Linux Docker 沙箱执行：无网络、只读根文件系统、无宿主挂载、非 root、删除 capabilities、no-new-privileges、进程/内存/CPU/输出限制。只经 stdin 输入局部 bundle/code、stdout 输出 JSON，容器执行后销毁，并核对实际隔离配置。AST 检查只是入口检查，不替代沙箱。

独立验证器仍在宿主侧执行，未交给生成代码修改：原图/解析文件哈希、输入 ID、原生轴线支持、边界、格网覆盖、合并关系、分隔线保留、文字归属及引用均受原有约束。来源需要另行直接重读 DXF，并查看宿主从原图生成的 source/overlay。结构通过不等于人工签认，更不等于工程语义验收。

旁路 run 即使后续人工确认也不能直接 `promote`，返回 `SHADOW_RUN_NOT_PROMOTABLE`。只产生 experimental 方法包。后续公共方法需独立变更审查、明确适用范围、跨图纸正反例及兼容性验证，再经明确的正式接入；本轮没有执行该步骤。

## 本次真实证据（2026-10-16）

`artifacts/dynamic-shadow-near-axis-20261016/` 保存策略、模型提示及返回元数据、代码、执行和验证结果、原图叠加、来源独立核验、错误候选拒绝以及哈希清单。

ChatGPT 直接订阅 GPT-6.1 Sol 对 case60-33 生成 1 次：输入 8,777、输出 1,047，总计 9,824 token。代码 SHA 为 `55eae9fdada26a81048f35bceb597ddad34242320e587b4ac527688ec3cab0e8`。随后同代码本地重放 19 个局部，新增模型调用为零。

5 张源图纸：结构通过 10 个，输入拒绝 6 个，非表格 1 个，仍未解决的真实表格 2 个。旧动态已通过的 9 个全部保留；新通过 case60-33 共 27 格，原组件为 19 格。这是分格修正收益，非本轮新增从无结果到有结果。孔浦 11816-36 的原组件未解决→26 格能力为旧版已有，27/28 仍返回 NON_RECTANGULAR_CELL_COMPONENT。

第二阶段实测使用真实 `CadAgentSession.invoke_tool('read_table')` 触发冻结重放，工具响应、证据及观测完全一致。没有额外运行完整 LLM 问答回合；自动触发动态生成分支另有本地模拟测试。因此不能将本次记录称为完整自主问答验收。


## 多方法范围扩展（2026-10-16）

可修改、调试的是各类方法独立的 `adapt(bundle)` 函数：文本筛选与连续原文片段、原生线段长度、两线段最短距离、原生简单闭合轮廓与面积，以及已有表格网格。模型可以重写实现和局部计算策略，运行失败或独立验证拒绝时收到本方法代码与错误，在同一预算内修正。公共组件、输入构造器、验证器和执行环境仍由宿主管理。

`GET /capabilities` 返回当前方法、输入范围、触发工具、输出契约、验证器和隔离方式。扩展方法的新增注册点为 `contracts.py`（Family/SPECS）、`validation.py`（独立验证）、`capabilities.py`（可触发工具）及对应正反例测试。未注册的 method 或不适用的工具名在请求阶段拒绝。

每个策略的 target 可独立指定 `method`、`instruction`、`diagnostic_run_id`、`reuse_run_id` 和触发方式。仍共用策略级模型、预算和有效期。一个局部可以配置多种方法，target/run/observation ID 按方法隔离；同一次读取命中的多个方法在后台依次执行。执行代码时不持有工作区总锁。

```json
{
  "idempotency_key":"region-methods-unique",
  "mode":"dynamic",
  "model_profile":"siwc_sol",
  "instruction":"按各方法契约处理用户选定局部，保留原生来源。",
  "agent_shadow_enabled":true,
  "max_runs":2,
  "max_model_calls":6,
  "max_reserved_tokens":192000,
  "targets":[
    {
      "snapshot_id":"snap_...",
      "method":"text_spans",
      "layout_id":"layout_...",
      "bbox":[0,0,100,60],
      "instruction":"提取包含指定材料或尺寸的完整原文，不能改写。",
      "trigger":"authorized_read",
      "trigger_tools":["query_entities","read_source_context"]
    },
    {
      "snapshot_id":"snap_...",
      "method":"segment_distances",
      "layout_id":"layout_...",
      "bbox":[0,0,100,60],
      "instruction":"计算局部原生线段之间的最短距离；只使用图纸单位。",
      "trigger":"authorized_read",
      "trigger_tools":["query_spatial","read_geometry","measure_geometry"]
    }
  ]
}
```

返回策略中的 `targets[].target_id` 由宿主生成。显式试运行改为 `POST /policies/{policy_id}/trials` + `{"target_id":"adtarget_..."}`；旧 `{"table_id":"table_..."}` 路径仍只选择表格网格目标，旧幂等键与请求摘要兼容。调用方不能借 trial 请求更换方法、坐标、模型或传入 raw code。多方法 replay 可为每个 target 指定自己的 `reuse_run_id`，系统核验来源运行的方法一致。

两种触发模式明确分开：

- `host_failure` 为默认：表格使用原组件失败；所有方法均可使用同输入、同方法且有独立拒绝记录的 `diagnostic_run_id`。显式区域的 `not_applicable` 基线不算失败，不会凭空启动生成。
- `authorized_read`：宿主已经明确授权该方法和范围的试验，匹配一次正常读取即可触发。该授权写在策略中，LLM 不能通过工具参数自行开启。结果仍为旁路候选，不代表原组件失败。

范围匹配来自实际工具参数及可信来源：layout+bbox 必须与白名单精确相同；read_source_context 的 region 必须来自当前快照且被范围包含；read_geometry/measure_geometry 的全部 entity IDs 必须属于该局部原生输入。measure_geometry 的 length、distance、area 分别映射长度、间距、闭合轮廓方法。无法解析范围、无效调用、跨范围、未知实体不触发。不会从问题文字猜测坐标或扩大范围。

读取行为不改变：旁路代码、结果和错误不进入当次问答证据。范围外读取、未配置 policy、重复读取保持原来的执行路径；队列不可用时正常工具结果仍返回。当前仍是单进程有限并发，另一任务占用适配器或订阅连接时保留 BUSY 拒绝记录，不强行抢占，也不切换提供方。

`segment_distances` 验证器用宿主 Shapely 独立重算原生有限线段最短距离，覆盖交叉、重叠、平行和端点最近情况；输出保留两条原生来源与 drawing_unit。距离不等于墙厚或门宽。闭合轮廓复核现叠加真实多边形而非外包框，间距叠加所选线段和宿主重算的最近点连线。

本轮验证目录：`artifacts/dynamic-multimethod-20261016/`。人工构造输入与真实历史图纸严格分开，前者验证方法合约和自动调试流程，后者验证兼容性，不能据此宣称新增工程识别准确率。来源敏感范围的既有外发授权没有自动扩大；本轮新生成仅使用人工构造矩形及测试文字。

### 多方法界面与本轮验证（2026-10-16）

图纸工具栏新增“局部方法调试”：打开时冻结当前视野的布局和 bbox；选择原文片段、原生线段长度、线段最短间距或闭合轮廓与面积。表格详情入口另外提供表格网格。填写目标后可直接运行一次，或授权当前 Agent 会话在匹配读取时旁路运行。界面策略固定使用 ChatGPT 直接订阅 `siwc_sol` / `gpt-6.1-sol`，单策略一个试验、最多三次生成或修正、四小时失效；关闭授权停止后续触发，已启动任务另行取消。页面刷新后不自动恢复此前授权；服务端策略仍按原过期时间失效。

该入口允许生成和调试五类 `adapt(bundle)` 方法函数。生产组件、输入构建器、沙箱执行器、验证器、权限与验收规则均不交给生成代码修改。后续新增方法仍需宿主补上契约、验证器和范围匹配规则。一般 Agent 读取没有匹配授权范围时不会启动试验。

`artifacts/dynamic-multimethod-20261016/review.html` 保存本轮证据：四次真实 GPT-6.1 Sol 请求，共 6822 tokens；四类构造样本首次生成通过，五项冻结代码留出重放通过，三次真实 Agent 工具调用的返回／证据／观察不变。此前四次订阅忙碌在 HTTP 前拒绝，未切换其他模型。真实生成没有触发修正；错误反馈再生成由模拟回归测试覆盖，不能称为本轮真实修正证据。

原 DXF 独立重读核对 12 条原生线段与 4 条原文；20 个错误候选被拒绝。历史 19 个表格局部和 3 个非表格方法共 22 项兼容，六项输入拒绝保留。历史长度报告新增的 `basis` 字段是此前已存在的来源说明变化，原始计算输出相同。此轮新模型生成仅使用人工构造区域，不新增私有图纸外发，也不作为真实图纸新增恢复收益。候选尚未自动采用或注册。

复核 API 对渲染副本使用局部显示原点，避免百万量级坐标导致栅格叠加线消失；显示原点写入 provenance，输入／源文件／验证结果的原始坐标不变。旧脚本调用 `source_svg` 的默认坐标约定保留。
