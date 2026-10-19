# 可替换 AgentRuntime 与 Harness 对照

本里程碑仅修改 cad_opt_agent，保留原生运行时为默认实现。DeepSeek Harness 和 Codex Harness 都启动实际官方运行时进程，初次对照统一调用 DeepSeek `deepseek-flash`；现已支持按请求选择 Qwen 配置，见 [多模型说明](qwen-models.md)。同一对照组不替换模型。

## 模块边界

| 模块 | 职责 |
| --- | --- |
| `analysis/session.py` | 当前图纸身份、允许的 CAD 工具、参数与范围检查、调用预算、来源登记、最终 JSON/引用/数值校验 |
| `analysis/agent.py` | 原 AgentRunner 的兼容入口，交给运行时注册表选择实现 |
| `runtimes/base.py` | `AgentRuntime.run(session, history) -> dict` 协议 |
| `runtimes/native.py` | 原有工具循环，默认实现与可选择的回退 |
| `runtimes/external.py` | 两种真实 Harness 的初始化、事件处理、工具回传、最终校验与一次纠正 |
| `runtimes/rpc.py` | 有总时间限制的 stdio JSON-RPC 与子进程清理 |
| `runtimes/bridge.py` / `messages.py` | Responses / Messages 的文本与函数调用适配，统一转入已有 ModelGateway |
| `runtimes/dsh_tools.mjs` | 静态、人工编写的 DeepSeek Harness CAD 工具插件，仅转发允许的工具 |

CAD 解析、砌体抽取、事实存储、来源绑定和网页原图定位继续复用。这里的运行时接口不接受任意代码、命令或文件路径。

## 版本与安装

- DeepSeek Harness：官方 npm `@deepseek-ai/dsh@0.2.0-rc.2`。直接使用官方 SDK stdio 协议，因为本机从官方 PyPI 查询不到文档所述 Python SDK 发布包。
- Codex：本机已升级至 `codex-cli 0.162.1`（历史对比使用 0.144.5），适配器在启动时校验精确版本。没有改变用户的 Codex 配置或登录状态。
- `harness/package.json` 与 `package-lock.json` 固定 DeepSeek 依赖；`.harness/` 是被忽略的仓库内安装目录。

安装 DeepSeek Harness：

```sh
python -m scripts.install_harness
```

该命令使用仓库内缓存、`npm ci --ignore-scripts`，不安装到全局。需要可用的 Node/npm。Codex 需要 PATH 中存在上述版本；版本不符明确失败，不自动改装用户的全局 Codex。

官方参考：

- [DeepSeek Harness SDK](https://github.com/deepseek-ai/deepseek-harness/tree/master/packages/sdk)
- [DeepSeek Harness 架构](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/architecture.md)
- [Codex app-server](https://learn.chatgpt.com/docs/app-server)
- [Codex 配置](https://learn.chatgpt.com/docs/config-file/config-reference)

## 接入方式与边界

DeepSeek Harness 使用 `sdk-minimal` 配置加静态补丁：禁用 bash、PowerShell、终端工具，挂载允许的 CAD 工具，不加载代码模式、网页工具或子代理。Codex 使用独立 CODEX_HOME、空工作目录、read-only、空环境选择、关闭 shell/exec/apply-patch/多代理/JS 等功能，通过动态工具回调使用同一工具集。

两种子进程只拿到短期的本地桥接令牌，不持有正式模型 API key。桥接仅绑定 `127.0.0.1`，校验令牌，使用现有后端模型密钥访问固定模型服务；请求模型不一致立即拒绝。真实模型只接收产品允许工具的原始 schema，原生 harness 附带的 plan/skills 等额外工具不会转发，模型返回额外工具也会在进入 harness 前被拒绝。工具最终仍通过当前图纸绑定的 `session.invoke_tool` 执行。

该模式没有向模型开放一次性代码执行。Harness 自身作为受信任的依赖代码运行，这不同于把整个依赖进程放进独立容器；本里程碑不声称实现了多租户 OS 安全隔离。

协议桥仅支持文本与函数调用，显式不支持多模态、托管工具和任意代理地址。Responses 的助手文字与工具调用会合并为 Chat Completions 的单条 assistant 消息，随后接对应工具结果，避免多工具回合 HTTP 400。

## 产品接口

`GET /api/runtimes` 返回运行时清单、安装状态和默认值。

`POST /api/chat-runs` 与 agent 模式的 `POST /api/chat` 增加：

```json
{
  "snapshot_id": "当前图纸快照",
  "question": "当前这张图纸是什么",
  "mode": "agent",
  "runtime": "deepseek_harness",
  "allow_fallback": false
}
```

`runtime` 为 `native | deepseek_harness | codex`，默认 `native`。回退默认关闭；显式允许后，所选运行时失败才以新的证据集合和预算运行原生实现，失败尝试保留在 `runtime_attempts`，返回 `runtime_requested`、`runtime_used`、`fallback_used`。回退总耗时和 token 包含失败尝试。实验脚本始终禁止回退。

外部运行时结果还包含 `harness_version`、`transport`、`model_bridge`（模型、实际工具名单、角色/调用 ID 结构及是否进入回答阶段），不保存 API 密钥。运行诊断位于当前 data_dir 的 `harness-runs/`。每次问答使用独立进程和状态目录，当前未复用热进程。

网页提供运行时选择与显式回退选项；仍保留本地事实查询。答案发布与引用点击走原有校验和定位链路。

## 验证与对照

```sh
# 普通回归；桥接鉴权测试需要允许本地监听端口
python -m pytest -q

# 实际 Harness + 模拟模型协议，仅合成图，不发真实模型请求
python -m scripts.probe_harness deepseek_harness
python -m scripts.probe_harness codex

# 实际 Harness + 实际 DeepSeek + 四份真实 DXF
python -m scripts.compare_runtimes --repeats 2 --output artifacts/runtime-comparison-final-20261014
```

真实实验使用六类固定问题、三种运行时、每题两次，共 36 次问答。图纸 SHA、初始事实集合、问题、领域提示、工具 schema、模型与非思考模式、输出 token 上限、请求/工具/总时间预算固定；按轮次轮换运行顺序。运行时自身附加的上下文和序列化方式不同，属于被比较实现的一部分。首次启动成本计入耗时；模型供应商缓存状态无法完全控制，因此两次重复只能说明小样本表现。

脚本生成逐项 JSON、manifest、summary.json 与 report.html。每条引用核对 snapshot、原文字符范围、实体 ID 和 SourcePart；关键词断言仅作开发回归，不作为语义正确率。金额未估算，以服务返回 token 作为用量记录。

第一批包含 Codex 协议适配错误的记录保留在 `artifacts/runtime-comparison-20261014/`，不能当作最终运行时能力结论。修正后的正式开发对照位于 `artifacts/runtime-comparison-final-20261014/`；它仍是探索性验证，不是独立业务验收。

完成后的量化结果、失败原因与语义复读发现见 [运行时对照结果](runtime-comparison.md)。

## GPT-6.1 Sol 订阅桥接对照

在完成独立 SIWC 登录后，可通过 `/api/chat-runs` 使用 `model_profile=siwc_sol`、`runtime=deepseek_harness` 或 `runtime=codex`、`allow_fallback=false`。该组合启动真实固定版本 CLI，经本地受限桥接调用公开 Responses 订阅接口，复用自定义运行时的流式聚合和模型校验；不使用付费网关或 Codex 登录凭据。网页的订阅配置默认仍选择自定义循环，本次新增组合主要用于 API 对照测试。

两个harness的模拟完整循环测试可用 `CAD_TEST_EXTERNAL_HARNESSES=1 .venv/bin/python -m pytest tests/test_siwc_harness.py -q` 执行；会启动本地CLI和回环监听，但使用模拟模型响应，不消耗订阅。完整两题对照及限制见 [订阅验证记录](siwc-subscription.md)。
