# 真实图纸几何流水线 E2E

固定清单是 `tests/fixtures/real_geometry_tasks.json`：理工 10# 楼 (`ligong-east`，147.6 MB) 和 D1–D3 楼 (`property-bath`，66.7 MB)，来源均为本仓库历史 artifact 中保存的完整 DXF。清单固定源 SHA-256、WCS 选区、毫米假设，以及 16 个冻结参考对象。原始文件只读。

```sh
.venv/bin/python scripts/evaluate_real_geometry.py \
  --backend spawn --backend local --repeats 2 --retain-data \
  --output artifacts/geometry-isolation-real-e2e-20261019
```

默认执行 cold、warm、alternating 三阶段；可用重复的 `--phase`、`--backend` 限定。`--retain-data` 将本次新导入的数据保留在输出目录的 `data/<backend>`，要求目录为空。没有该参数则使用自动清理的临时目录。禁止把生产 `.data` 作为输出。保留数据供后续模型轮复用时，应说明该模型轮复用了本次新导入结果，不应再称为又一次全新导入。

每个后端使用新的 Workspace，通过生产 HTTP `/api/drawings` 上传完整 DXF 并重新运行生产解析器和来源索引。显式测试配置 `max_upload_bytes=256 MiB`，不代表默认 32 MiB 接口支持此大小。OfflineSettings 只覆写 model_catalog 以绕过 `.env`；模型字段空白，账户目录位于新建测试目录，没有读取生产配置或账号。

调用链为 HTTP component geometry → 完整记录 HTTP 回读与分页 → 实际 `CadAgentSession.invoke_tool(query_building_relations)` → observation → `finish` 答案校验与 TaskLedger 保存 → HTTP task 回读。确定性脚本写交付候选提示，不调用模型，因此运行模式固定为 `geometry_pipeline_e2e`，不能称为真实 LLM E2E。每次保留 graph、delivery、session 分阶段指标与 geometry executor 事件。

LocalGeometryExecutor 与 spawn 执行器使用同一当前算法。每个计时任务前删除**该测试数据库**中的 component 和 measurement 派生结果，确保计时包含真实几何运算，不能用缓存结果返回时间冒充 worker 性能。cold 另清内存缓存并重启 spawn worker；local 清 worker-native 派生缓存。OS 文件缓存不清。warm 按图纸分组，alternating 交替两图；至少 repeats=2 才能体现不同访问顺序。缓存字节上限保持生产默认值，大文件可能因超过预算无法缓存，这是被测行为。

指标定义：

- `complete` 是完整执行到交付并保存结果；`contract_pass` 另核对源哈希、分页、HTTP/tool 记录一致、task 回读、observation 引用和实际 geometry 调用。
- 所有尝试进入分母，导入失败使该图全部计划任务失败。几何异常保留错误与用时；没有将失败样本剔除。p50 是中位数，p95 是 nearest-rank，小样本应连同样本数阅读。导入耗时单列，不混入工具延迟。
- local/spawn 比较完整 graph 的规范化哈希，只去掉时间及文件 stamp。几何失败视为无法比较，不算“一致”；几何完整但交付失败仍可独立比较已落盘graph，并保留交付失败。
- 精确率/召回率严格标作 `reference_precision/reference_recall`，表示冻结参考集的一致性。参考文件原始声明为 `agent_manual_review_of_native_DXF_linework_and_labels_not_independent_human_acceptance`，并非独立人审真值，也不是盲测。样本是历史排查选择集，存在选择偏差，不能外推全图准确率。
- 空间匹配按原有 180 图纸单位距离阈值，但改为按距离排序的一对一匹配，避免重叠预测重复挣得 true positive。identity 必须与参考 door/window 相同。召回分母是全部16个标注对象；precision 分母仅为这些标注邻域内明确 door/window 的预测；未标注范围预测另记未评分，不能据此计算全图precision。未知/漏检不算正确。
- 汇总每图每后端只取一次参考评分，重复阶段不扩大独立样本数。工程独立 accuracy、全图 precision/recall、面积误差保持 null，因为没有独立真值。旧算法的面积、轮廓及旧输出绝不当作正确答案。

报告保存源 SHA-256、代码和 runner/manifest 哈希、运行环境，检查运行期间生产代码是否变动。这里的比较隔离了执行位置；不能据此归因所有架构变化，也不证明工程验收。

长图运行可在已写入结果后中断，用相同输出目录加 `--resume --retain-data --repeats 1` 缩减尚未执行的重复。恢复必须通过 manifest、生产代码和保留 DXF 哈希校验；已记录成功或失败不会重跑或删除，原始 HTTP 导入耗时保留，报告添加 resume_history。恢复前已完成的额外重复仍保留在分母里。失败操作的几何计时用本次 wall-clock；开始每次 spawn 操作先清 last_timing，避免拿上一成功操作的时间代替失败时间。

若生产代码在“仅导入成功、尚无几何观察”阶段进一步冻结，可保留这次导入：resume 仅在 service.py、reader.py、regions.py、contracts.py 哈希全部不变时允许跨冻结恢复，旧全量代码哈希保留为 prior_import_code_hashes，并逐条给旧导入标注代码哈希。几何阶段使用最终进程重新加载的代码。可加 `--freeze-ready <path>`，文件必须包含与当前全部生产 Python 文件相同的 `code_hashes`；marker 不存在或不匹配会拒绝启动，避免旧进程等待期间持有旧模块而误称新版本。外部应在冻结后启动该命令。

本次实际运行 repeats=1，warm/alternating 顺序重合，不能分别验证交替访问退化。为了不重复数分钟解析，local 的隔离库从本轮首次HTTP导入复制经哈希校验的四个原始/解析来源artifact和drawing记录，未复制任何component/measurement结果；本次证据是 import-reuse-plan.json。后续可在命令中添加 `--reuse-import-backend spawn` 自动复现此方式，且应同时指定 `--retain-data`、spawn先运行。local HTTP返回的是本轮已有同源快照，完整解析耗时只统计首次，不能把快速复用称为全新导入。

修正版实际命令（生产算法未改，修正确定性请求分页）：

```sh
.venv/bin/python scripts/evaluate_real_geometry.py \
  --backend spawn --backend local --phase cold --repeats 1 --retain-data \
  --reuse-import-root artifacts/geometry-isolation-real-e2e-20261019/data/spawn \
  --provenance-report artifacts/geometry-isolation-real-e2e-20261019/revision1-evaluation.json \
  --freeze-ready artifacts/geometry-isolation-real-e2e-20261019/freeze-ready.json \
  --output artifacts/geometry-isolation-real-e2e-20261019/revision2
```

初轮20项工具页产生一次RESULT_TOO_LARGE失败，之后一例为修请求而主动中止；两者保留，后者不归因于几何产品失败。修正版每次HTTP/工具页均limit=1，读取全部页，并在delivery前落盘完整graph与参考评分。source_geometry、source_descriptors等操作的完成时间逐项写run.log；summary区分几何完成、交付完成和整体契约通过。原始完整导入成本仍为首轮成本，修正版两后端独立复制原始/解析artifact，不执行第二次新解析。仅四次cold最终评测，不再把同序warm/alternating当独立实验。

特别限制：本轮两图16个参考ID全部来自历史old-O已有输出，未包含原始参考清单在D5/D7图纸上的6处已知漏检。任何16/16结论仅是已识别目标的冻结参考一致性，不代表困难目标召回、全图识别准确率或独立工程验收。

交付恢复用独立命令：

```sh
.venv/bin/python scripts/evaluate_real_geometry.py \
  --delivery-recovery artifacts/geometry-isolation-real-e2e-20261019/revision2
```

它只对“几何已完成、交付未完成”的同次graph创建新的临时任务，分页limit=2，保留未知对象，校验全量ID、每页少于50,000字符和12次调用预算。NoComputeExecutor拒绝任何隐藏几何重算。恢复结果单独保存至delivery-recovery/evaluation.json，原始cold失败、原几何耗时和graph哈希不改写；恢复成功不追溯修改原端到端通过率。

最终结果见[本轮结果报告](../artifacts/geometry-isolation-real-e2e-20261019/REPORT.md)：只执行4次cold，几何4/4完成、两图local/spawn graph均一致；原交付2/4完成。两次ligong预算失败保持原样，使用同次graph、limit=2的[独立交付恢复](../artifacts/geometry-isolation-real-e2e-20261019/revision2/delivery-recovery/evaluation.json)均完成，8页完整保留15对象（含1unknown），没有重算几何。最终原始观察在[revision2/evaluation.json](../artifacts/geometry-isolation-real-e2e-20261019/revision2/evaluation.json)。16个历史已识别正例各后端全部匹配，仅是冻结参考一致性；不是工程准确率。初轮大小失败、主动中止与首次完整导入provenance均继续保留，详见结果报告链接。
