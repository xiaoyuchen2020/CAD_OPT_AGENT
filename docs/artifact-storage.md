# 测试记录与图纸保存策略

`artifacts/_shared/sha256/<前两位>/<完整 SHA-256>.<扩展名>` 是测试图纸的共享存储。
内容相同的 DXF（包括不同文件名）只保留一份；内容变化时生成新对象。
DWG 和图纸派生文件 `scene.json`、`entities.json`、`regions.json`、
`render.json`、`geometry.json`、`oda.json` 同样按原始字节去重。
不同解析器版本产生不同内容时各自保留，不按图纸名称或 snapshot ID 合并。

测试目录保存相对符号链接，因此既有代码仍可读取
`data/drawings/<snapshot_id>/source.dxf`。链接不是文件副本。
共享对象只读；更新图纸索引必须原子替换链接，禁止直接打开链接写入或修改共享对象。
`Store.write_json` 已实现独立替换，不会改变其他实验读取到的内容。
普通 `.data` 工作区不受测试目录策略影响。

新的批量测试在 `manifest.json` 的 `drawings.*.references` 保存输入路径、
SHA-256 和大小；每次 attempt 的 `input-references.json` 保存相同信息。
其中 `files.*.path` 相对于 `relative_to` 所指的输入目录。
新的导入记录含 `artifact_references`（路径相对于该快照的图纸目录）。
这些引用描述输入版本；之后生成的派生缓存以当前文件引用为准。
历史数据库和结果报告保持原字节，迁移映射记录在 `_maintenance/*.json` 和 `.jsonl`。

脚本应调用 `copy_artifact` / `copy_artifact_tree` 复制输入，不能使用普通
`shutil.copy2` / `copytree` 复制图纸；普通文件仍按正常方式复制。
冻结代码保持原样；旧冻结脚本可能仍会复制文件，运行后需执行以下整理命令。
在仓库根目录执行（不调用模型）：

```sh
.venv/bin/python -m scripts.compact_artifacts --root artifacts
.venv/bin/python -m scripts.compact_artifacts --root artifacts --apply --clean-caches
```

默认只审计。`--apply` 校验哈希后才原子替换副本，并记录迁移日志；
重复执行可继续整理。`--clean-caches` 仅删除 `__pycache__`、`.pytest_cache`、
`.pyc`、`.DS_Store` 和顶层 `npm-cache`。不会按日期删除实验。
有任何缺失或损坏的输入会记录错误并以非零状态退出。

旧校验清单可能包含 `.DS_Store` 或 `.pyc` 的哈希。清理缓存前，
会将含这些条目的 `*manifest*.json`、`*sha256*.json`、`*hash*.json`
原样备份到 `_maintenance/<时间>/original-manifests/`，然后从工作清单中移除缓存条目，
同步这些清单之间的哈希引用。每项变更、原哈希、新哈希和备份位置均记录在
`manifest-migration.json`。实际源代码和图纸输入的校验值保持不变；
原始历史清单的字节及完整哈希仍可从备份核验。

保留运行记录、结果、失败记录、报告、截图、SQLite 数据库、冻结代码和哈希清单，
它们用于回放及对比。删除一个实验目录只移除该实验的引用；
不要直接删除 `_shared`，也不要只复制一个实验目录后删除原共享库。
搬迁或备份应连同整个 `artifacts` 保存，或显式导出引用的共享对象。
没有可靠的完整引用集合时，不自动清理共享对象，也不自动删除旧实验结果。
