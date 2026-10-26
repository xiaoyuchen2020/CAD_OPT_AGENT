# DWG 内网转换与导入

`POST /api/drawings` 继续接受 multipart `file`，支持 `.dxf`、`.dwg`（大小写不敏感）。DWG 上传后由后端将原始二进制发送至配置的 ODA `/jobs`，轮询状态，下载同任务的 `/result` 和 `/dxf`。源文件限 32 MiB；转换后的 DXF 和 ODA JSON 各限 256 MiB，避免压缩 DWG 展开后误用原文件大小限制。

## 配置与运行

- `CAD_ODA_BASE_URL`：内网 ODA 服务，默认 `http://192.168.1.146:18080`。
- `CAD_ODA_API_KEY`：后端私密配置，无默认值。不会出现在健康接口、页面、模型请求或失败消息中。
- `CAD_ODA_TIMEOUT_SECONDS`：上传、排队、转换、下载总等待预算，默认 600 秒。单次连接最长 5 秒，单次网络读写等待最长 30 秒。

客户端不使用环境代理、不跟随重定向、不自动重提转换任务。只请求配置的服务和校验后的任务 ID，不跟随响应中的外部下载 URL。同一工作区串行导入，其他已有图纸查询不因网络等待而持有工作区锁。官方试用授权限流与到期会明确提示；无本地或公共云转换回退。

`GET /api/health` 的 `dwg_conversion.configured` 仅表示配置存在，不证明 ODA 当前在线或授权可用。`supported_formats` 为 `dxf,dwg`。

## 成功与失败

只有以下步骤全部完成，才发布图纸记录：

1. 原 DWG 文件头及大小验证。
2. ODA 任务成功，上传、状态和结果中的原 DWG SHA-256 一致。
3. JSON schema 2.0 结构有效，DXF 字节数和 SHA-256 与元数据一致。
4. 本地 DXF 读取及来源索引建立成功。
5. 保存原始 DWG、转换 DXF、ODA JSON、场景和来源索引，最后发布图纸记录。

转换/下载/校验/解析失败以结构化 `error.code/message` 返回。页面持续显示错误并终止当前导入，不切换当前图纸、不自动提取；原有图纸与结果保留。后续提取失败会沿用已有提取错误处理，与转换成功分开。

可识别但存在代理对象、缺失字体、展开数量上限等诊断的 DXF，仍沿用现有 `partial` 状态；这不是转换失败，也不代表已完整识别工程内容。工作台现有 80,000 展开图元上限保持不变。

ODA 提取器 `20261026.1` 支持无目标块定义、且无附带属性的空块引用：原始 DWG 保持不变，ODA JSON 保留对象句柄、位置及 `NULL_BLOCK_REFERENCE` 诊断，导出 DXF 的内存副本排除这些无法展开的引用。JSON 的 `dxf_export.omitted_null_block_reference_handles` 记录具体句柄；后端校验其对应原始实体后，在图纸诊断中显示 `ODA_NULL_BLOCK_REFERENCE`，图纸标记为 `partial`，`index_coverage.traversal_complete=false`。不会为缺失块定义推断几何。空块引用若带有属性，提取器仍停止导出，以免丢失附带文字。

## 来源与下载

- `name`、`source_format`：用户上传的名称和格式，DWG 导入保持 `DWG`。
- `original_sha256`、`original_size_bytes`：原始上传文件。
- `sha256`、`size_bytes`、`parsed_format=DXF`：实际参与解析、证据定位和几何计算的 DXF；保持旧证据字段含义。
- `conversion`：ODA 任务 ID、SDK/schema/extractor 版本、原文件/DXF/JSON 哈希，不含访问密钥。
- DWG 快照 ID 同时绑定 DWG 哈希、转换 DXF 哈希和解析器版本，不会混用直接导入的同内容 DXF 快照。

下载接口：

| 路径 | 内容 |
| --- | --- |
| `/api/drawings/{id}/source` | 用户上传的原始文件，DWG 或 DXF |
| `/api/drawings/{id}/dxf` | 实际解析的 DXF |
| `/api/drawings/{id}/oda-json` | ODA 原生 JSON，仅 DWG 转换快照提供；原生 DXF 返回 404 |

已成功导入的相同 DWG 在同一解析器版本下复用本地快照，核对三份文件的哈希，不重复消耗官方转换调用，也不依赖服务端 24 小时结果保留。缓存缺失或损坏会报错并终止，避免复用错误快照；没有成功记录的失败导入可以直接重新提交。

JSON 下载为 ODA 实体数据，不是工作台的事实 JSON。JSON 尚未完全表达 HATCH、完整标注、视口及代理对象语义，因此现有场景、来源索引和问答仍使用转换 DXF。

## 验证

```bash
.venv/bin/python -m pytest -q tests/test_oda_import.py tests/test_integration.py
node --check web/app.js
# 明确上传指定图纸到已配置内网服务；输出保存在隔离验证目录。
.venv/bin/python -m scripts.check_oda_import /absolute/path/to/drawing.dwg
```

自动测试涵盖正常转换与三种下载、DXF 兼容、来源身份/缓存、401/429/503、连接错误、超时、转换失败、授权不可用、响应异常、来源/输出哈希不匹配、体积限制、无效 DXF 和失败后不产生图纸/提取任务。
