# 轻量混合检索（2026-10-15）

## 启用与回退

只修改 cad_opt_agent。默认仍是原有 keyword 检索，砌体与通用事实、原图快照、证据 ID、三个运行时的工具白名单均保留。没有 AR024 策略、视觉识图或一次性代码执行。

```sh
# 在本仓库内，不覆盖现有 .env
.venv/bin/python -m pip install -e '.[test,retrieval]'
.venv/bin/python -m scripts.prepare_embedding
.venv/bin/python -m cad_agent
```

网页“选择检索方式”选择“混合检索 · 语义增强”。恢复“关键词检索 · 原有流程”即可回退。后端未升级时，新前端根据 health 能力字段禁用新选项，不向旧后端发送不认识的参数。

本地 BAAI/bge-small-zh-v1.5 不需要额外 API key。准备脚本从官方模型仓库解析具体 revision，只下载配置、词表和 safetensors，生成 `cad-model.json` 文件哈希清单。当前下载 revision：`7999e1d3359715c523056ef9478215996d62a620`。请求期间禁止自动下载模型，`trust_remote_code=False`，CPU 运行。

可在 .env 配置 `CAD_EMBEDDING_MODEL_DIR`，默认是本仓库 `.data/models/bge-small-zh-v1.5`。依赖和模型缺失、损坏或向量生成失败时，返回**原有关键词结果**，同时记录 `effective_mode=keyword` 和 `fallback_reason=embedding_unavailable`。界面明确显示回退。模型文件存在只表示准备过，不表示运行时健康检查通过。

本次模型文件及依赖仅下载到本仓库 `.data/` 与 `.venv/`，没有更改系统 Python 包。源图纸只读校验并复制到隔离测试目录。向量化过程本地执行；真实问答测试仍使用原有 DeepSeek 账户发送相关文字。

## 模块边界

- `cad_agent/adapters/embedding.py`：独立、延迟加载的本地编码器，模型文件完整性检查、归一化向量、实际 tokenizer 用量、最多 256 个查询缓存。
- `cad_agent/analysis/retrieval.py`：现有来源区域分段、中文二元字串/字母数字词的 BM25 排序、稠密向量相似度、RRF 融合和按来源区域去重。
- `DrawingIndex.search(mode=...)`：keyword 保持旧逻辑；lexical 是同一结构分段上的关键词排序对照；hybrid 是 lexical + embedding；semantic 供只读检索 API 诊断。
- `ChatRequest.retrieval_mode`：keyword（默认）/ lexical / hybrid。会话内所有 search_sources 使用同一选择，模型不能自行切换，适用于三个运行时。
- `GET /api/drawings/{snapshot_id}/retrieval?q=...&mode=hybrid`：单独的受限检索接口，支持 layout_id/frame_id/section_id/offset/limit。旧 `/sources` 列表接口完全保留。

## 结构和来源

复用解析器已有条款/表格行区域与 context_region_ids，不重新判断表头或适用关系。短区域完整保留，长区域最多 300 字符、步长 240；同布局同图框的最多两个解析器父区域各截取少量文字作为检索上下文。它不是新的表格或章节解析器，也不对来源重新建立事实。

结果包含 region_id、chunk_id、snippet、snippet_start、text_span、原布局/图框和两路排名。snippet 始终是原始 region.text 的精确切片。相关度不是事实置信度，不产生新 evidence_id，不把检索上下文或模型输出注册成原文证据。Agent 继续用 read_source_context 按 snippet_start 读取，再走现有引用校验。

筛选先于两路排序；match=all 保留对所有 query/keywords 的字面匹配约束。每路最多 60 个候选，按 RRF `sum(1/(60+rank))` 合并，再按 region_id 去重。此处 total 是候选数量，不代表全图覆盖或完整匹配数量。没有人为设相似度阈值来伪装“不存在”。无依据问题也可能返回候选，须由后续阅读判断。

派生向量缓存写入工作区 `retrieval/*.npz`，键包含快照、分段版本、模型 revision/编码约定和全部索引文本。使用原子替换、禁止 pickle；无效缓存重新构建，旧原图和证据不修改。范围不同使用不同派生缓存，MVP 不部署向量数据库。

## 用量与成本口径

- 聊天 usage：模型服务实际返回的 prompt/completion/total tokens。
- embedding usage：本地 tokenizer 实际计算的输入 token，含查询前缀及特殊 token，文档与查询分开记录。不是聊天模型计费 token，不相加冒充账单。
- index build：首次构建单独计时；再次读取缓存记 input_tokens=0。
- query cache：命中时本地新增编码 token=0。
- 工具 trace、成功结果和失败运行均保留 retrieval_usage；UI 分开展示本地 embedding token。
- 模型服务失败未返回的用量属于未知，不当作零计费。

## 复现

```sh
.venv/bin/python -m pytest -q
node --check web/app.js
.venv/bin/python -m scripts.compare_retrieval
# 发送真实样本相关文字到已配置的 DeepSeek：
.venv/bin/python -m scripts.compare_retrieval --live --live-only
# 仅实验内对 HTTP 503 做最多两次重试，保留首轮结果：
.venv/bin/python -m scripts.compare_retrieval --live --live-only --live-label live-retry --retry-busy 2 --case 59-chase,59-table,62-water,62-absent
```

固定问题位于 `tests/fixtures/retrieval_cases.json`，基于四份已验证 SHA-256 的真实 DXF。这些样本含相近版本，不能视为四个独立项目或泛化验收。gold 在排名前由明确原文片段绑定区域和证据；来源标签为开发者审阅，非独立业务金标。

检索实验三组使用相同 query 和 keywords，轮换运行顺序，各测三次。首查和缓存后时间分开；这些时间不包括每次新建 DrawingIndex 的时间，端到端问答时间才包含会话初始化。单步检索命中率不能等同于 Agent 最终正确率，因为 Agent 会自行调整检索词和调用其他工具。

结果与逐项复核见 `docs/hybrid-retrieval-results.md`，原始产物在 `artifacts/hybrid-retrieval-20261015/`。

## 依赖依据

- BAAI 模型与查询前缀：https://huggingface.co/BAAI/bge-small-zh-v1.5
- Sentence Transformers 编码与归一化：https://www.sbert.net/docs/package_reference/sentence_transformer/model.html
- RRF 思路：https://www.elastic.co/docs/solutions/search/hybrid-search
