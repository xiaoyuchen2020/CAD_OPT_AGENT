# 文本、元数据与证据锚点

本实现保留 `Evidence` 存储结构及永久 ID。模型上下文是后端证据的投影，不能建立新的来源、读取回执、对象身份或工程适用性。

## 来源锚点

`evidence.verify_source_evidence()` 是快照内统一校验入口：快照、区域、Unicode code point 字符跨度 `[start,end)`、逐字引文和永久 ID 必须一致。引用前不做空白、标点或 Unicode 归一化。定位字段与源区域不一致时拒绝；缺失字段由 `bind_evidence()` 重新构建。数值目录从原来源重建，不能信任输入的数值解释。`Evidence.unit` 描述图纸坐标单位，不等于所有引文数字的单位。

会话注册、恢复任务以及来源页缓存共用校验；同一永久 ID 的冲突不能覆盖先前证据。注册复用 DrawingIndex，并检查索引文件签名；来源改变要求重新读取。原有发布前快照验证继续执行。档案的字符分页仍不授予新证据资格。

原始文件摘要与解析版本已参与快照身份。当前不跨版本自动重定位：新图纸或重新解析结果必须读取并生成新证据。前后文模糊匹配尚未进入产品路径，不会通过同文或相近坐标继承旧证据信用。

## 请求内原文目录

`analysis/source_context.py` 提供共享的 `ReferenceAliases`、`RequestSourceCatalog` 和 `render_source_line()`。运行时目录只接受会话账本中的证据，并检查输入副本与账本身份一致；独立序列化单测可使用显式可信输入模式。

普通 Agent 保留会话级别名映射，但每次请求重建原文字典。来源记录 review 使用资料包级别映射，局部纠错沿用映射且重新输出可见记录的原文。两者使用相同 JSON 转义及原文格式，字面文本中的 `e1`、换行或伪引用标记不会变成引用身份。

只按相同 evidence_id 去重。不同来源即使逐字相同也分别定义。已存储 ID、未读页或候选目录不能被自动提升为本请求可见原文；原有已读工作集恢复路径仍只来自真实观测。最终预算整理禁用历史工作集恢复，不能重新装回已裁掉来源。

来源记录 `auto` 模式直接由程序编译答案，无模型 token 收益可声称；`review` 模式复核选择，仍不能自由改写正文。

## 元数据任务策略

`analysis/context_policy.py` 从现有需求类型、操作与实际观测工具组合策略，允许同一任务同时需要来源、表格、数值、计算及空间信息。没有类型计划、未知需求或未知工具使用保守视图。策略只影响表示，不授予工具执行或事实完成资格。

已知文字/表格工具可以省略顶层预览和高亮操作；未知字段与未支持工具保留并报告投影诊断。来源标题、表头、单位依据、条件、例外、分页、冲突和工程适用性状态保留。空间任务保留定位元数据；计算任务保留数值身份和单位来源。来源做法投影保留新增语义字段及表头证据，不再依靠字段白名单默默丢弃它们。

`ProjectedObservation` 分离可见载荷、所需原文证据和所需工具观测。`bounded_packet()` 在预算内也检查依赖是否齐全，按完整依赖组裁剪；表格数值不能丢掉表头/单位来源单独留下，计算不能丢掉所引观测。依赖环有访问集合，缺失依赖记入 `context_dependency_gaps`；遗漏数量保留在 `context_omissions`。原本就缺失的单位继续是缺项，不推断默认值。

最终请求大小由 `final_context_budget.fit_messages()` 按编码后实际字符数检查。字符数不是服务 token；收益必须另看订阅服务实际报告的用量，并同时核查语义覆盖。

## 验证入口

`tests/test_source_context_contract.py` 覆盖锚点篡改、Unicode、定位恢复、别名生命周期、引用可见性、混合任务和预算依赖。现有上下文、来源工作流、业务表格及最终预算测试继续执行。

`scripts/validate_text_anchoring.py` 在独立数据目录复制指定真实 DXF/解析快照，保存输入和代码哈希、来源核验、模型视图与运行结果：

```sh
python scripts/validate_text_anchoring.py \
  --source-data /path/to/frozen/data --snapshot snap_... \
  --question '列出指定做法原文' --mode auto \
  --output artifacts/anchoring-validation
```

`--reimport` 使用当前解析器重新导入复制的完整 DXF。`planning_metrics` / `building_schedule` 模式直接运行产品解析入口并与 `--reference` 的独立原图参考对照，不把它记为 Agent 端到端通过。

`review` / `agent` 模式要求显式 `--account-data`，沿用现有订阅账号队列，`allow_fallback=false`、`source_read_batching=false`，不复制凭据。不因质量失败挑选重跑。来源锚点通过、流程发布、分项完成和真实语义验收分别记录；有限样本不能证明全项目工程适用性。
