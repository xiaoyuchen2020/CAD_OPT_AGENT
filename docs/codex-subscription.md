# Codex 订阅本机试验接入

仅在本机 MVP 中使用官方 Codex App Server 的 ChatGPT 登录。新运行时为 `codex_subscription`，模型配置为 `codex_sol_56`（`gpt-5.6-sol`）、`codex_sol`（`gpt-6.1-sol`）或 `codex_astra`（`gpt-6-astra`）。原 DeepSeek/Qwen API 配置、Codex 桥接运行时保持独立。

## 使用

1. 打开 8766 页面，选择“Codex 订阅 · GPT-5.6 Sol”（本次已验证推理及工具调用）。运行时会自动选择“Codex 订阅 · 本机试验”，并关闭付费 API 回退。
2. 展开“Codex 订阅 · 本机连接”，点击 Continue with ChatGPT，再打开官方登录链接。
3. 如果官方页面出现 `Unexpected token '<'` 或 HTML/JSON 错误，可选择“使用设备码登录”。在系统浏览器打开官方验证页面，输入本次设备码；不要分享设备码。切换登录方式会取消上一次未完成的登录。
4. 自行完成官方登录后点击“刷新登录状态”。只有显示已连接后，才开始问答。
5. “断开本产品连接”只登出本产品的隔离登录态。不会调用桌面 Codex 的登出接口。

账户登录不是模型权限证明。页面列出的模型是配置的请求目标，并不表示订阅通道支持；实际可用性仍以成功的推理结果为准，不自动替换为另一模型。

## 实现和边界

- `cad_agent/runtimes/subscription.py`：登录管理、独立订阅运行时、工具白名单、用量记录。
- 官方 CLI 当前固定版本 `0.162.1`（此前为 `0.144.5`），使用 `initialize`、`account/read`、`account/login/start`、`account/login/cancel`、`account/logout`、`thread/start`、`turn/start` 和事件接口。已从本机 CLI 导出实验协议核对必要字段。
- 支持 `chatgpt` 浏览器登录和 `chatgptDeviceCode` 官方设备码流程；本产品不处理密码，不读取、复制桌面端的 auth.json，不自行调用非公开推理接口。
- 官方 CLI 自行保存和刷新凭据。应用的隔离目录为当前 `CAD_AGENT_DATA_DIR/codex-subscription/`（验证服务位于 `artifacts/hybrid-retrieval-20261015/data/codex-subscription/`）。目录权限 0700，登录后认证文件权限收紧为 0600；这些路径被 Git 忽略。
- 启动子进程时不继承 DeepSeek/Qwen API 密钥和桌面 Codex 配置。没有为此向 .env 添加 OpenAI API key。
- 只提供现有九个 CAD 动态工具，关闭 shell、unified_exec、apply_patch、multi_agent、js_repl、web search；拒绝额外服务端请求。工作目录为空、只读、不允许审批升级；不开放一次性代码执行。
- 复用现有工具结果精简、重复调用复用、证据注册和最终校验。达到工具/无进展预算时中断探索，另开无工具线程，仅用已读证据整理答案；最多再做一次整理/引用修复，不循环纠正。
- 同时最多一个订阅问答；保留会话 180 秒时间上限和 16 次工具上限。已读取证据且报告 Token 达 30,000 时结束探索；无论是否有证据，达 40,000 时结束探索。有证据且剩余时间不足 45 秒时也转入收尾。总量 50,000 为软预算目标，预留一次无工具整理；用量异步到达可能超出目标，结果明确记录 soft_limit_exceeded，不因答案末尾用量越线丢弃已生成答案。最终整理不再继续探索或循环修复，提示词最多 24,000 字符，并保留已读条款的结构关系。预算收束不能被当成证据充分。
- 直接使用官方 App Server 推理时，不能像自建 API 桥接那样逐个拦截其内部模型 HTTP 请求。本实现不声称精确控制底层请求次数，也不覆盖官方客户端的传输重试行为。
- `thread/tokenUsage/updated` 的累计值按线程转换成增量，避免重复统计。UI 标明订阅计费来源；这些 Token 不能直接换算成 API 账单或剩余订阅百分比。

## 接口

- `GET /api/codex-account`：连接状态、套餐类型、登录是否待完成；不返回邮箱、账户 ID 或 token。
- `POST /api/codex-account/login?flow=browser|device`：官方登录入口。设备码仅返回给当前本机页面，不写入报告或日志。
- `POST /api/codex-account/logout`：取消待完成登录并断开产品连接。
- 问答参数：`runtime=codex_subscription`、`model_profile=codex_sol|codex_astra`、`allow_fallback=false`。

真实账户登录和所请求模型的调用验证需要用户完成官方认证。未登录、模型无权限、订阅额度不足均不静默回退到付费 API。

初始实现验证：143 项本地测试通过，前端语法检查通过，CLI 实验协议和未登录状态读取通过。当时尚未执行真实模型调用，记录位于 `artifacts/codex-subscription-20261015/verification.json`。

2026-10-15 单题订阅验证：用户已完成设备码登录，账户状态为已连接、Pro。仅提交一次真实 DXF 问题 `59-chase`（“埋电线时能用锤子在墙上凿槽吗？”），选择 `gpt-6.1-sol`，关闭付费 API 回退。官方 Codex 本地日志返回 HTTP 400 / `invalid_request_error`：`The 'gpt-6.1-sol' model is not supported when using Codex with a ChatGPT account.` 产品回合失败，CAD 工具调用为 0，没有答案或引用，也未收到 Token 用量事件；用量未知，不能记为零消耗。工具调用、证据引用和订阅 Token 记录均尚未通过真实模型验证。未自动重跑，未切换模型；Astra 未测试。一次产品回合不等于官方客户端内部请求次数。完整脱敏记录位于 `artifacts/codex-subscription-20261015/sol-single/`，其中 `verification.json` 保存结论，`run.json` 保存产品运行结果。

## 官方依据

[Codex App Server](https://learn.chatgpt.com/docs/app-server) 说明本地/开源应用的托管登录、设备码及 RPC 接口；该认证方式不用于商业或托管服务。面向多人分发或商业化时，应另行采用官方 [Sign in with ChatGPT](https://developers.openai.com/siwc/quickstart) 方案并核实应用资格。

[模型目录](https://developers.openai.com/api/docs/models) 确认目标模型 ID；不能据此推断本账户已获得对应订阅权限。

### GPT-5.6 Sol 单题验证（2026-10-15）

新增独立配置 `codex_sol_56`，保留其他模型 ID。20 项相关本地测试及前端语法检查通过。通过 8766 产品接口仅提交一次同一真实 DXF 问题 `59-chase`，未开启付费 API 回退，未重跑。

模型成功完成 3 次检索和 1 次原文上下文读取，找到“严禁锤斧剔凿”的条款以及“墙体工程 / 墙体设计要求”的结构上下文；本地逐段回查确认条款对应的 2 个 DXF 实体文本一致且具有定位坐标。工具返回生成 8 个证据，但最终答案和引用未发布：第 5 个用量事件使累计 Token 达到 60,122，触发 50,000 Token 软上限（`CODEX_TOKEN_BUDGET`）。这是收到用量之后的阈值，不能保证精确封顶。包含轮询的耗时约 36.176 秒。

已报告输入 59,525、输出 597、总计 60,122 Token；输入中缓存命中 43,904，输出中推理 168，均不可重复相加。记录来源为官方 App Server，计费通道为订阅；这些数字不等于订阅额度消耗百分比或 API 美元费用。此次证明模型访问、CAD 工具调用和 Token 事件记录可用，但完整带引用问答尚未通过。下一步应提前停止探索并预留最终回答预算，而非仅放大上限。记录：`artifacts/codex-subscription-20261015/sol56-single/verification.json`。

### CLI 升级与模型目录（2026-10-15）

Homebrew CLI 从 0.144.5 升至 0.162.1，并同步产品版本校验。使用原产品登录态，官方 `account/read` 仍确认已连接 Pro；`model/list` 返回 7 个可见模型：`gpt-6.1-sol`（默认）、`gpt-6-astra`、`gpt-6-sol`、`gpt-6-luna`、`gpt-5.6-sol`、`gpt-5.6-terra`、`gpt-5.6-luna`。本次未发起真实模型推理，目录可见性不等于已完成调用验证。此前 GPT-6.1 Sol 被拒绝仅代表旧版 CLI 测试结果，不能外推到新版环境。产品菜单仍为已配置的 3 个订阅模型，并非上述完整动态目录。

33 项相关本地测试通过；新 CLI 的本地模拟协议探针完成 CAD 工具调用和引用校验（响应来自固定本地模拟，不是 DeepSeek 或 OpenAI 真实推理）。记录位于 `artifacts/codex-subscription-20261015/cli-upgrade/`。本轮未处理先前的 Token 预算停止问题。

### GPT-6.1 Sol：预算收尾修复后的单题验证（2026-10-15）

仅提交一次同一真实 DXF 问题 `59-chase`，新版 CLI 0.162.1、`gpt-6.1-sol`、hybrid 检索、efficient 策略，禁止付费 API 回退。运行 `chat_279d1be11b2a417a9746a595db0d7f72` 成功完成。

- 1 次检索、3 次原文上下文读取；发布 1 条 derived 结论和 3 个原文引用。
- 结论：墙体水电管线暗敷开槽不能用锤斧剔凿，应按原文使用轻型电动切割机辅以手工镂槽器，并在墙体砌完、砂浆强度达到设计要求后进行；不扩大到其他构件。
- 逐条引用接口返回 200；3 个引用的文字跨度与来源一致，所对应 5 个原始 DXF 实体文本、定位框均与解析记录一致。条款的两个结构父标题均被引用，适用范围保留为 derived。
- 官方 App Server 报告 3 个用量事件，总计 34,081 Token：输入 33,753（含缓存命中 19,328），输出 328。耗时 27.805 秒，含轮询观测 28.145 秒。
- 停止原因为 `model_answered`，没有触发备用整理分支。不能用此单次成功声称已在真实模型上覆盖越线收尾；该分支由本地模拟回归覆盖，包括迟到用量、越线答案保留、停止后拒绝新工具以及最多一次无工具整理。151 项本地测试全部通过。

| 运行 | CLI / 模型 | 发布答案 | 工具调用 | 已报告总 Token | 含轮询耗时 |
|---|---|---|---|---|---|
| 修复前单题 | 0.144.5 / GPT-5.6 Sol | 否，Token 软上限中止 | 4 | 60,122 | 36.176 秒 |
| 本次单题 | 0.162.1 / GPT-6.1 Sol | 是，3 个引用 | 4 | 34,081 | 28.145 秒 |

模型、CLI 和收尾逻辑均有变化，以上是运行记录对照，不能把差值单独归因于本次修复或据此评价模型优劣。Token 不是订阅消耗百分比，也不等同于 API 美元费用；embedding 在本地执行，不计入上述订阅 Token。

完整记录：`artifacts/codex-subscription-20261015/sol61-budget-fixed-single/`，包含提交前实现哈希、源文件 SHA-256、运行记录、耗时与逐条证据验证。
