# 真实订阅与 DWG 转换验收 · 2026-10-27

本次使用同版新界面的独立实例（8782），复用项目已有的 SIWC 订阅授权和 ODA 转换配置。数据保存到独立验收目录，没有更改 8765 预览实例的连接配置，也没有使用业务工程图。

## 结果

| 检查 | 结果 |
| --- | --- |
| 真实 GPT-6.1 Sol Low 订阅问答 | 通过：responses_subscription，model_access_verified=true，billing=siwc_subscription |
| 图纸原文读取与引用定位 | 通过：地上外墙的材料、B07、A5.0、240厚、M5 专用砂浆与合成原文一致；2 条引用的来源及局部预览可读，浏览器定位成功 |
| 不确定性保留 | 原文未给出“240厚”的适用单位依据，回答保留缺口，分析任务标为 partial；问答运行本身 completed，不将其误记成所有字段已确认 |
| 真实 DWG 外部转换 | 通过：从新界面上传 ODA 项目生成的合成 DWG，得到 DXF 与 JSON，原图预览及文字索引正常 |
| 文件完整性 | 原始 DWG 15,851 字节、转换 DXF 75,630 字节、JSON 3,297 字节，三个下载的 SHA-256 均与来源/转换记录一致 |
| 重复与损坏文件 | 同文件重复导入复用同一快照及转换任务；无效 DWG 返回 400，没有新增图纸 |
| DWG → 真实订阅 → 引用定位 | 通过：逐字读出 `ODA API evaluation fixture`，引用属于转换后的 DWG 快照，浏览器定位成功；任务 completed |
| 付费回退与批量读取 | 两次问答均关闭；使用记录来自真实 SIWC subscription，没有使用付费 API |

第一题耗时 71.696 秒、8 次模型请求、累计 217,431 tokens；DWG 端到端题耗时 36.593 秒、5 次模型请求、累计 87,586 tokens。累计 token 包含每轮重复输入。第二题经历一次 finish_analysis 校验未通过，模型修正后成功发布；没有跳过校验。

本次验证标准 GPT-6.1 Sol Low，未据此推定 High、Luna 等所有模型档位都已通过。合成 DWG 只有一段文字和一条线，证明外部转换及下游链路连通，不代表复杂工程图的完整转换质量。

## 附带修复

设置窗口原先依据“模型辅助提取”是否配置，笼统提示填写 DeepSeek API Key；即使订阅问答实际已连通也会显示。已改为区分所选问答连接、本地提取和订阅授权，浏览器刷新确认生效。65 项前端回归、脚本语法检查和 diff 检查通过。

## 可复核记录

- 汇总与哈希：`artifacts/live-services-20261027/report.json`
- 真实订阅运行：`chat_29c7b1e7d506437ab31bb43f24e960d1`
- DWG 端到端运行：`chat_7c29a575d749432f9b41b664195ced27`
- 转换任务：`36e3a34484db4aa83006228b91a24555`
- 截图：`subscription-evidence.png`、`dwg-converted.png`、`dwg-subscription-evidence.png`（同一验收目录）
- 验收服务脚本：`scripts/validate_live_workspace.py`；使用原授权存储，不复制或输出凭据。
