# 框选样本查找相似对象

`cad_agent.analysis.similarity_search.search_similar_objects(workspace, snapshot_id, args)`
是只读入口，`args` 接受 `SimilarObjectSearch` 或对应字典。模块不调用模型、外部服务或付费
API，也不写入工程对象注册表。`geometry.compute` 的 `similarity_search` 静态操作在已有隔离
进程内执行，读取、展开和比较整体超时 20 秒，并尊重当前请求更短的剩余预算。

工具栏“框选找相似”支持拖动选取样本；已选图框时搜索该框，否则搜索框选前的当前视野。候选卡片可定位原生路径高亮，显示轴向包络并明确其不代表规格。HTTP 入口为 `POST /api/drawings/{snapshot_id}/similarity-search`，Agent 工具为 `search_similar_objects`。已绑定任务范围时，样本和搜索范围都须在该范围内。切换图纸、布局或图框后，旧请求不再更新当前界面。

```python
args = SimilarObjectSearch(
    layout_id="当前快照的layout ID",
    sample_bbox=[100, 200, 120, 220],
    frame_id="",                         # 可选，必须同一快照/布局
    search_bbox=[0, 0, 1000, 1000],       # 可选，与frame范围取交集
    limit=12,
    scan_budget=4000,
    candidate_budget=400,
    min_similarity=0.78,
)
result = search_similar_objects(workspace, snapshot_id, args)
```

所有 bbox 均为 **DXF WCS `[xmin, ymin, xmax, ymax]`**，不是画布像素。样本必须完整包含
至少一个块实例或端点连接形状，并在有效搜索范围内。框选穿过其他形状时返回
`status="sample_unresolved"` 和 `sample_issue`。仅被图框包围、但图框边线不穿过样本，
不会产生此错误。相邻文字不是形状，不能仅凭文字框搜索。

搜索只访问指定布局；图框/搜索范围按完整对象的真实包络包含关系过滤，不裁掉范围外的兄弟
图元以构造更小的假对象。底层遵循已有 `walk_entities` 的实例路径和
`stable_id("ent", snapshot, layout, "/".join(path))`，重复块、嵌套块和阵列实例不会合并。
块叶图元按直接实例前缀分组；非块图元按端点在原图单位小数点后六位相同连接成组。不补缝，
不把附近文字、墙体或者断开的线自动附会到对象。

匹配方法为本地归一化几何距离分布：源曲线按包络最大边长的 1/128 离散，在形状路径上进行
32 个等弧长采样，将归一化点对距离排序比较，并辅以图元种类组成、块名、图层及同实例内精确
文字。算法原则上忽略平移、旋转和等比缩放，曲线离散会产生小幅数值差异。它没有学习型图像
embedding，不解释型号，不确认工程对象类型；镜像或不同形状也可能得到相近分数。

返回字段：

| 字段 | 含义 |
| --- | --- |
| `status`, `sample_issue` | `ready` 或 `sample_unresolved`；后者明确说明样本问题 |
| `sample` | 完整样本的实际来源和几何，同候选结构，不包含相似分数 |
| `candidates` | 按 `similarity` 降序的候选；分数是相似度，不是类型置信度 |
| `object_id`, `instance_id`, `instance_path` | 当前快照中稳定的检索对象与实例标识；非块实例 ID 为空 |
| `source_entity_ids`, `source_entity_paths` | 实际源图元 ID/路径；`entity_ids` 为兼容高亮别名 |
| `bbox`, `actual_size_wcs` | 原生曲线包络及其轴向宽高，单位为顶层 `unit`；不代表工程标注尺寸 |
| `source_root_transform` | 顶层 INSERT 的旋转、缩放、插入点、块名；嵌套/阵列叶几何已变换到 WCS |
| `highlight`, `highlight_paths`, `component_boxes` | 布局/bbox/IDs、近似 WCS 路径及各图元框，用于显示定位 |
| `relative_path_length_scale` | 离散路径总长之比，明确标为近似，不能替代源块缩放或规格 |
| `engineering_confirmed` | 始终为 `false` |
| `coverage` | 访问根实体数、扫描数、候选预算、截断原因、诊断及返回数 |

预算：源 DXF 最大 32 MiB；访问根及虚拟实体默认 4,000、最多 20,000；比较候选默认 400、
最多 2,000；返回最多 50；总离散点预算 100,000；每对象最多 4,096 点和 256 个源实体。
源文件超过字节预算直接拒绝，其他预算截断保留 `coverage.truncated` 和
`truncation_reasons`。部分展开的块根不发布；扫描截断时所有非块连接组都不作为完整对象，
因为未访问线段仍可能连接到组上。候选预算按源顺序取前缀，只保证已比较候选内排序，不承诺
全布局全局最相似。外部参照、代理图元、填充、标注和无法展开的几何明确保留诊断；
`full_drawing_enumeration` 始终为 false。

返回的“真实包络”指当前源几何的 WCS 包络；库对曲线求界有数值近似。高亮路径同样用于定位，
不是精密测量边界。源文件在计算前后校验 `file_stamp`；变化时拒绝结果。没有持久化缓存或
模型请求。

验证：`python -m pytest -q tests/test_similarity_search.py` 覆盖重复门形状与不同形状、
旋转/缩放、阵列实例路径和源 ID、高亮范围、布局/范围/图框隔离、框选跨边界、图框包围、
扫描/候选/结果预算、扫描截断的散线组、过期源文件及真实隔离进程入口。
