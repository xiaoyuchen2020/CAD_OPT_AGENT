# LLM 问题理解、精确执行与证据校验

默认开启 `CAD_LLM_QUERY_INTENT=1`。自然语言请求先进入所选模型运行时；模型在首次业务调用中提交结构化意图并选择工具，本地校验参数后执行。`CAD_LLM_QUERY_INTENT=0` 保留旧路由，供明确的兼容回放使用。订阅运行时继续禁止付费 API 回退，批量来源读取仍须请求显式开启。

## 执行协议

1. 模型可先读取图纸/项目概览。首次业务调用为 `search_source_records` 或 `plan_task`，附带 `query_intent`。
2. 服务端校验意图的用户原句绑定、要求覆盖、目标 ID、否定条件及历史来源，保存稳定的 `intent_id`。失败不执行正文读取。
3. `search_source_records(target_id)` 按对象名称和编号定位标题候选。范围单独评估：supported / conflicting / unresolved；未明候选保留，不把未知当作不存在。不读取做法正文、不展开地下室所有做法类型。
4. 模型选择候选返回的 `read_assembly` 参数，明确读取正文及后续分页；需要上下文、表头、单位或几何时继续选择对应工具。
5. `finish_analysis` 保留现有来源、数值、单位、字段归属和交付校验，并增加目标编号、范围来源、多目标覆盖、内容筛选及未读分页检查。失败返回具体修正信息；无法确认时交付 partial / unknown / conflict。

例如“帮我找下地下室内墙4做法”的首次调用：

```json
{
  "target_id": "wall4",
  "query_intent": {
    "action": "lookup",
    "targets": [{
      "target_id": "wall4",
      "requirement_id": "q1",
      "subject": {"value": "内墙", "question_quote": "内墙4"},
      "identifier": {"value": "4", "question_quote": "内墙4"},
      "conditions": [{
        "kind": "scope", "operator": "include",
        "value": "地下室", "question_quote": "地下室"
      }],
      "requested_content": ["做法原文"]
    }],
    "unresolved_requirements": []
  }
}
```

`action` 支持 lookup / enumerate / compare / calculate / measure / locate / mixed。对象名称开放，不用一套关键词词典决定所有问题。条件区分 scope 与 content，支持 include / exclude。局部规范化包含全半角、空白和少量等价标题名称；不等于跨类别扩展。编号使用边界匹配，内墙4不会命中内墙40或内墙4A。

只有模型显式填写 `alternatives` 和 `expansion_reason` 才会执行额外名称检索；额外候选保留 alternative 标记，不改变原始目标。候选分页上限为8，结果记录原图范围、标题位置、源文件签名和真实读取参数。零结果仅说明索引标题未命中。

## 意图、历史与证据

意图是用户要求，不是图纸证据。`value` 必须绑定实际问题中的 `question_quote`。历史条件必须指定 `source=parent`，只能引用同一选图集合的父问题，不能引用助手的推断。一个任务的已登记意图不可静默改写；检索调整使用显式 alternatives，新问题通过父会话创建新任务。恢复任务复用原意图、候选和读取状态。

`query_target_ids` 将结论绑定到目标。同一要求含多个目标时，每个目标均须回答或明确缺口。编号目标须引用自己的标题证据，不能拿另一编号或单独的材料层代替。范围使用 `evidence_roles.scope`，必须来自同快照、同图框；同图框只是一项必要检查，并不证明做法适用于工程实例。命中排除范围的原文不能作为满足条件的 observed / derived 结果。内容排除须读取完整的选中记录，不能用未读或零命中证明不存在。

本地校验无法证明任意自然语言解释的正确性；模型仍负责语义判断。对复杂同义范围、否定表达或表头归属无法直接确认的情况，保留未决状态，而不授予工程身份或全图穷尽结论。

## 上下文和失败处理

- 新模式不在模型调用前自动执行做法路由或房间批次，不在交付时暗中重跑房间批次或补读原文。
- 候选存入任务，但工作上下文只带候选数量和最近检索收据；完整记录仍可审计。
- 已读做法最多保留最近6个显式选择页面作为可恢复工作集；源证据和归档仍持久化。
- 有裁剪标记的检查点及最终整理包不会再次注入完整历史页面。预算根据实际编码后消息计算，保留完整引文及遗漏计数，无法容纳必要信息时明确失败。
- 首次意图错误、过期候选、编号错误、范围证据错误、未读分页和遗漏目标分别返回 QUERY_* 信息。意图与候选登记随失败调用回滚。
- `responses_subscription`、订阅桥接和 Codex 订阅最终整理共享预算处理；各运行时共享同一工具执行与答案校验层。

## 验证

`tests/test_query_intent.py` 覆盖默认开关、模型先行、精确编号、排除、跨图框与跨快照、来源改变、分页、历史及恢复、多目标交付和最终上下文。旧的固定工具脚本显式关闭新模式，避免把旧脚本当作新模型行为验证。

真实图纸验证入口为 `scripts/validate_query_intent_real.py`。它将产品代码和真实快照冻结到隔离目录，输出代码/图纸哈希、请求、运行、任务和追踪；使用已有订阅账号与共享并发锁，不复制凭证，不启用回退。

```sh
.venv/bin/python scripts/validate_query_intent_real.py \
  --source-data /absolute/path/to/source/data \
  --account-data /absolute/path/to/subscription/account \
  --snapshot SNAPSHOT_ID --out artifacts/query-intent-validation
```

`--offline` 是明确的本地固定问题回放，不能作为真实模型通过证据。真实验证默认问题为地下室内墙4，可用 `--question` 改变。`--parent-run-id` / `--resume-task-id` 复验历史与恢复；同一冻结目录用 `--case-name` 保存不同案例，避免覆盖先前结果。代码变化后必须使用新输出目录。运行 completed 只说明流程交付完成，还须检查任务 partial / unknown、引用和图纸原文；候选归属不应记作工程适用性验收通过。

父会话续问只接受已完成的 run；失败但已有证据的任务用 `--resume-task-id` 恢复，不把失败 run 当作 `--parent-run-id`。共享账号繁忙时可用 `--queue-timeout` 单独延长排队等待，不改变模型推理预算。保存结果可用 `scripts/audit_query_intent_validation.py` 核对模型先行、真实原文引用、目标覆盖和正文关键项；审计不会发送模型请求。

本次实现的版本、真实图纸运行与回归边界见 [2026-10-26 验证记录](llm-query-intent-validation-20261026.md)。
