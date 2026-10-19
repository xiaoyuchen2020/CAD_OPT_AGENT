# 固定本地架构评测

运行：

```sh
.venv/bin/python scripts/evaluate_architecture.py \
  --repeats 3 \
  --output artifacts/architecture-upgrade-20261019/evaluation.json
.venv/bin/python -m pytest tests/test_architecture_evaluation.py -q
```

脚本只支持 `synthetic_contract`。它在临时目录独立生成三张 DXF，直接调用生产 parser、region builder、DrawingIndex、证据绑定与 GeometryService；不创建完整 Workspace，不加载 Settings、账户配置或 `.env`，不使用生产 `.data`，不调用模型或网络。临时数据在完成后删除，JSON 保留每个源 DXF 的 SHA-256、单位、布局、图框、解析诊断与全部任务观察。

固定输入和人工 golden 在 `tests/fixtures/architecture_tasks.json`。三张图的内容、图框范围和单位不同：毫米、米、未知单位。10 个任务覆盖三图原文检索、精确引用和原文 span、两次闭合矩形面积计算、未知单位拒绝、分页全量覆盖、独立布局限定、跨图内容隔离和词法检索。面积期望由矩形的独立公式 6×4=24 m² 与 7×3=21 m² 给出；期望值不从被测输出生成。这里测的是几何轮廓面积，不是房间净面积或工程语义。

每个任务分别在三个阶段重复运行：

- `cold`：每例清空内存派生缓存。OS 文件缓存、落盘 geometry 与 measurement 记录保留，不能称为完全冷启动。
- `warm`：按图纸分组，在前一阶段已执行的缓存和落盘状态下运行。
- `alternating`：按 manifest 中 alpha/beta/gamma 交替访问的固定次序运行。

JSON 分开记录 `complete` 和 `correct`。例如有完整面积结果但值与 golden 不符，记完整且错误；异常记不完整且错误，并保留异常类型、消息和用时。所有尝试都进入分母与延迟统计，失败不剔除。p50 用中位数，p95 用 nearest-rank；阶段内任务异质，统计用于复现检查，不用于宣称普遍加速倍数。每个观察包含任务 ID、阶段、重复号、耗时、校验结果和证据摘要，便于按同一任务配对比较。

报告带 Python、平台、ezdxf 版本、manifest 哈希，以及评测脚本、manifest 和全部 `cad_agent/*.py` 的代码哈希。生成的 DXF 可能含库写入的时间或 GUID，因此同一输入规格重跑的原始文件哈希不保证相同；它标识该次实际来源。JSON 明确标记 `runtime_mode=synthetic_contract`、`model_calls=0`。

这套小型合成集验证代码契约和可复现失败归因，不代表真实图纸准确率、真实模型答案正确率、工程验收或大图性能。当前直接测量本地函数的 wall-clock；不伪造 session/model/validation 分阶段指标。若后续引入已标注真实 replay，应独立定义 manifest、许可来源、golden 和 runtime_mode，不能混入该合成结果。

## 真实图纸几何流水线评测

真实图纸采用独立协议：[real-geometry-evaluation.md](real-geometry-evaluation.md)。固定样本是两张完整真实 DXF 的指定范围，先通过生产 HTTP 上传接口在隔离测试 Workspace 重新导入，再执行真实几何工具、来源回读、确定性交付和 task 回读；LocalGeometryExecutor 与 spawn 使用冻结的同一算法。结果模式是 `geometry_pipeline_e2e`，不代表真实 LLM 问答。默认几何操作超时和缓存预算保持生产配置，超时和其他失败均进入分母，不以抬高超时或删失败改善结果。

报告位于 [revision2/evaluation.json](../artifacts/geometry-isolation-real-e2e-20261019/revision2/evaluation.json)，源导入与冻结切换的证据在 [pre-freeze-import-checkpoint.json](../artifacts/geometry-isolation-real-e2e-20261019/pre-freeze-import-checkpoint.json) 和 [freeze-ready.json](../artifacts/geometry-isolation-real-e2e-20261019/freeze-ready.json)。实际状态以 JSON 的 imports、observations 和 summary 为准；运行期间 JSON 是增量检查点。

参考标注是历史代理对原始 DXF 线条/标注的冻结复核集，不是独立人工工程真值。报告只将相应 precision/recall 称为选定参考集一致性；独立工程准确率、全图 precision/recall 和独立面积误差不计算。两图少量重复的 cold/warm/alternating 延迟不能证明统计显著差异，无法外推到所有图纸或所有硬件。后续真实模型轮应使用单独运行模式和结果，不能与此确定性几何流水线混算。

真实轮运行时部分阶段与 root 的小型合成回归套件并行，负载背景记录在 [runtime-context.json](../artifacts/geometry-isolation-real-e2e-20261019/runtime-context.json)。这些耗时是本次运行观察，不是无干扰微基准，也不足以证明统计显著的后端速度差异。

本次实际 `repeats=1`。因此 warm 和 alternating 的图纸访问次序相同，应视为重复访问轮次，不能分别验证缓存交替退化。为避免重复数分钟解析，local 后端使用本轮 spawn 阶段已首次完整 HTTP 新导入且校验哈希的 source/scene/regions/entities，复制到独立数据库；没有复制 component 或 measurement 派生结果。此过程记录在 [import-reuse-plan.json](../artifacts/geometry-isolation-real-e2e-20261019/import-reuse-plan.json)，模式为 `reuse_this_run_import`，完整导入成本只报告首次。

修正版最终轮位于 [revision2/evaluation.json](../artifacts/geometry-isolation-real-e2e-20261019/revision2/evaluation.json)，计划仅两图×local/spawn各一次cold（4次）。初轮理工图HTTP几何完成，但20项工具页触发 `RESULT_TOO_LARGE`，308.167秒失败完整保留在 [revision1-evaluation.json](../artifacts/geometry-isolation-real-e2e-20261019/revision1-evaluation.json)；第二图因修正分页主动中止，单列 [revision1-control-interruption.json](../artifacts/geometry-isolation-real-e2e-20261019/revision1-control-interruption.json)，不是产品几何失败。首图graph在旧runner下一例清理派生缓存时已删除，因此不能恢复评分或拼接其他graph冒充原308秒结果，修正版重新测量。

修正版HTTP与实际工具均从limit=1分页读完；HTTP geometry完成就先保存完整graph和参考评分，交付失败不会掩盖几何观测。summary分别列geometry_complete、delivery_complete和contract_pass；prior_revision和all_revision_attempt_audit保留旧请求失败与控制中止，不会只显示成功样本。由于大图native索引超过默认128MB估算预算无法保留，源解码/索引反复占用主要时间；本轮取消尚未执行的warm/alternating扩展，不为重复已知瓶颈扩大耗时，不能从cold-only结果推断热缓存性能。

本轮两选区的16个参考ID均为历史 `old-O` 输出对象；原冻结参考清单在其他D5/D7图纸中的6处已知漏检不在这两图里。因此本轮reference_recall的分母具有明显“已有输出”选择偏差，即使16/16也不能说明困难漏检已解决或真实识别召回率达到100%。

最终结果见[本轮结果报告](../artifacts/geometry-isolation-real-e2e-20261019/REPORT.md)：只执行4次cold，几何4/4完成、两图local/spawn graph均一致；原交付2/4完成。两次ligong预算失败保持原样，使用同次graph、limit=2的[独立交付恢复](../artifacts/geometry-isolation-real-e2e-20261019/revision2/delivery-recovery/evaluation.json)均完成，8页完整保留15对象（含1unknown），没有重算几何。最终原始观察在[revision2/evaluation.json](../artifacts/geometry-isolation-real-e2e-20261019/revision2/evaluation.json)。16个历史已识别正例各后端全部匹配，仅是冻结参考一致性；不是工程准确率。初轮大小失败、主动中止与首次完整导入provenance均继续保留，详见结果报告链接。
