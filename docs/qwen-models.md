# Qwen 多模型选择与 Harness 对照

## 配置与接口

已从用户指定的 cad_llms_group/.env 只读导入 Qwen 相关配置到本仓库 `.env`，没有修改来源仓库。密钥不进入网页、API 模型清单或测试报告。默认仍是 DeepSeek，问答模型按请求选择，互不修改全局模型配置；失败回退只更换 Harness，不更换所选模型。

| 配置 ID | 界面名称 | 当前服务模型 ID | 服务报告的上下文上限 |
| --- | --- | --- | ---: |
| default | 默认 | deepseek-flash | 沿用已有配置 |
| qwen_large | Qwen 大 · 长上下文 | Qwen3.6-27B-256k | 262,144 |
| qwen_medium | Qwen 中 | Qwen3.8-27B-FP8 | 16,384 |
| qwen_small | Qwen 小 · 短上下文 | Qwen3.8-27B-FP8 | 2,048 |

“大、中、小”沿用用户的服务档位叫法，不代表三个不同参数规模的模型。中、小服务的模型 ID 相同，上下文配置不同。以上模型与长度已通过各服务 `/models` 实时核对，原始非敏感记录在 `artifacts/qwen-runtime-comparison-20261014/model-discovery.json`。

重新导入：

```sh
python -m scripts.import_qwen_config /path/to/cad_llms_group/.env
```

脚本按来源仓库的调用约定读取：大模型使用 `MODEL_HOST`、`MODEL_NAME`，密钥依次取 `LLM_API_KEY` / `OPENAI_API_KEY` / `SMALL_MODEL_API_KEY`；中、小读取 `SMALL_MODEL_CHAT_*` / `SMALL_MODEL_EXTRACTION_*`、`SMALL_MODEL_NAME` 和 `SMALL_MODEL_API_KEY`。只写入本仓库的 `CAD_QWEN_LARGE_*`、`CAD_QWEN_MEDIUM_*`、`CAD_QWEN_SMALL_*` 变量，目标 `.env` 权限为 0600，且被 Git 忽略。配置改变后重启本地服务。

`GET /api/models` 只返回配置 ID、显示名、模型名、上下文上限和是否配齐配置，不返回密钥或服务地址。`configured=true` 不等于服务支持工具调用。

问答请求示例：

```json
{
  "snapshot_id": "当前图纸快照",
  "question": "当前这张图纸是什么",
  "mode": "agent",
  "model_profile": "qwen_large",
  "runtime": "codex",
  "allow_fallback": false
}
```

结果与问答记录包含实际 `model`、`model_profile` 和运行时。切换模型清除当前会话续问关联，避免无意携带上一个模型的问答历史。模型选择当前作用于工具问答；“模型辅助提取”继续使用默认模型，界面已有说明。

## 协议与失败边界

三个 Harness 都经过同一个 ModelGateway 使用所选 Qwen endpoint、密钥和模型。Qwen 请求设置 `chat_template_kwargs.enable_thinking=false`，DeepSeek 沿用 `thinking.type=disabled`。不更换模型来伪造 Harness 比较，也不以文本正则猜测并执行工具调用。

Qwen 当前聊天模板拒绝多个开头系统消息。适配层将这些消息按顺序合并为一条，保留全部内容，解决 Codex Responses 转换后出现的 HTTP 400；其他角色、工具参数与工具结果保持原有结构。

服务错误分类：

- `MODEL_TOOLS_UNSUPPORTED`：服务拒绝自动工具调用。应由模型服务维护者启用与当前模型模板匹配的工具调用解析配置，再复测。当前产品不会修改远端服务。
- `MODEL_CONTEXT_LIMIT`：上下文/输出预算不足。不静默截断证据，不偷偷切换模型。
- `ANSWER_VALIDATION_FAILED`：没有实际读取工具证据，或最终答案未满足统一契约。模型在正文中写工具名、伪 XML、Python 风格表达式不算实际工具调用。
- 非白名单工具名（包括带多余引号的名称）依旧拒绝，不替模型修正为另一个待执行操作。

小模型只有 2K 总上下文，本实验仍保留与其他配置相同的 6K 输出上限及完整工具定义；即使修复服务端工具开关，也还需单独验证上下文预算是否足够。当前主要失败发生在工具能力检查阶段，不能声称已测得小模型生成速度或 token 消耗。

## 复跑与可比性

```sh
python -m scripts.compare_model_matrix --repeats 2 --output artifacts/new-model-matrix
```

脚本要求新的输出目录，冻结 Python/静态工具插件代码并记录 SHA，所有配置使用同一快照运行，密钥仅通过进程环境传入，不复制 `.env` 到快照。仍使用前一轮四份真实 DXF 和六类固定问题，每题每种运行时两次，四个模型配置共 144 次；实验关闭回退和任意代码执行。

由于共享仓库的提示词和通用工具在此期间更新，最终实验重跑了 DeepSeek，而不是把旧六工具成绩直接拼入新结果。冻结版本及工具/提示词哈希可从每个配置的 manifest 检查；启动成本计入耗时，供应商缓存和服务器负载未控制，不作统计显著性声明。

每条保存耗时、模型、运行时、工具轨迹、已报告的用量、候选拒绝原因及引用检查。汇总分别给出发布成功数、来源/关键词通过数、成功耗时中位数、全部尝试耗时、已报告总 token 与用量报告次数。服务没有提供 usage 时标为缺失，不按 0 token 解释。自动通过仍不代表建筑语义正确率或独立业务验收。

最终记录位于 `artifacts/qwen-model-matrix-final-20261014/`，`report.html` 为矩阵汇总，各配置子目录保留逐题结果；接入冒烟和未冻结的早期记录位于 `artifacts/qwen-runtime-comparison-20261014/`，不混入最终结论。

完成后的成功率、耗时、token 与失败样例见 [最终对照结果](qwen-model-comparison.md)。
