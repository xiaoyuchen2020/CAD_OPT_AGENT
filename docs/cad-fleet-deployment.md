# CAD Atlas 单机双节点部署

本部署让 ODA 内网电脑承担上传、转换、任务排队、两组独立事实分析、证据归档及网页查看。产品仍只基于图纸回答事实问题：保留范围、来源、条件、单位和未知项；任务成功结束不等同于完整回答，也不产生工程优化或合规结论。

## 已采用的布局

| 路径或服务 | 用途 |
| --- | --- |
| `/opt/cad-atlas/current` | 指向部署源码版本，初始为 `/opt/cad-atlas/releases/20261028` |
| `/opt/cad-atlas/venv` | 服务使用的 Python 环境 |
| `/etc/cad-atlas/center.env` | 中心服务配置和内部访问令牌 |
| `/etc/cad-atlas/a.env`、`b.env` | 两组 agent/worker 的独立配置及模型访问配置 |
| `/var/lib/cad-atlas/cad-center` | 中心 SQLite、原始上传、结果及证据归档 |
| `/var/lib/cad-atlas/cad-a`、`cad-b` | 各自数据库、图纸快照、会话、沙盒配置及 Worker 检查点 |
| `/var/lib/cad-atlas/cache` | ODA 转换缓存、全机转换锁和模型并发锁 |
| `cad-atlas-center` | 中心服务，回环端口 8780 |
| `cad-atlas-agent-a`、`cad-atlas-agent-b` | 两组 agent，回环端口 8765、8766 |
| `cad-atlas-worker-a`、`cad-atlas-worker-b` | 各领取并执行一个任务 |
| nginx 端口 8080 | 统一内网页面入口，代理中心及受控的 agent 工作台 |

服务账户分别为 `cad-center`、`cad-a`、`cad-b`。共享缓存使用 `cad-fleet` 组、目录权限 2770 和文件权限 0660；agent 数据目录保持各自私有。配置由 root 管理，不通过网页返回或打印。ODA 的既有服务、配置与存储独立保留。

所有用户通过 nginx 的统一入口访问；不要额外把 8780、8765、8766 开放到内网。中心要求登录。访问 agent 工作台也经过中心鉴权；`/api/fleet-import` 和 `/api/fleet-runs` 仅供带内部令牌的 Worker 调用，不应通过浏览器代理开放。

## 模型和实例配置

按用户最终选择，A 使用 API GPT-6.1 Sol，B 使用 API Astra；两者使用 `native` 运行时，显式关闭模型回退。订阅登录不是此部署的运行前提。以下示例不含真实凭据：

```dotenv
# A 的关键项；B 改为 b、8766、cad-b 和 api_astra
CAD_AGENT_DATA_DIR=/var/lib/cad-atlas/cad-a
CAD_AGENT_PORT=8765
CAD_FLEET_NODE_ID=a
CAD_FLEET_URL=http://127.0.0.1:8780
CAD_FLEET_AGENT_URL=http://127.0.0.1:8765
CAD_FLEET_WORKER_DIR=/var/lib/cad-atlas/cad-a/worker
CAD_FLEET_CACHE_DIR=/var/lib/cad-atlas/cache
CAD_FLEET_MODEL_PROFILE=api_sol
CAD_FLEET_RUNTIME=native
CAD_FLEET_MODEL_CONCURRENCY=2
CAD_FLEET_CONVERTER_REV=20261028
CAD_FLEET_TOKEN=<由部署程序生成，与中心一致>
CAD_GPT_API_BASE_URL=<已授权的兼容 API 服务地址>
CAD_GPT_API_KEY=<由管理员安全注入>
CAD_ODA_BASE_URL=<现有 ODA 服务地址>
CAD_ODA_API_KEY=<现有 ODA 服务密钥>
```

API 单次响应读取等待上限由 `CAD_MODEL_READ_TIMEOUT_SECONDS` 配置，默认仍为 60 秒。Astra 可按实测设为 120 秒，并将 `CAD_AGENT_TIMEOUT_SECONDS` 设为 300 秒；每次请求的读取等待参数不会超过剩余任务预算，连接等待参数仍不超过 15 秒。配置必须为有限正数。`MODEL_READ_TIMEOUT` 明确表示读取响应等待超时；连接失败仍为 `MODEL_UNREACHABLE`。这属于 HTTP 单次读取等待限制，不能据此声称慢速持续传输一定在该秒数内结束；现有运行时还会在请求返回后核对任务总预算。修改配置后需要重启对应 agent，不能用配置存在代替真实模型成功证据。

中心的关键配置为 `CAD_FLEET_DATA_DIR`、`CAD_FLEET_TOKEN`、登录配置以及节点地址；以已安装的 `/etc/cad-atlas/center.env` 和 `cad_agent/fleet/app.py` 为准。不要把含密钥的环境文件提交到 Git、粘贴到日志或发给浏览器。

Worker 启动命令：

```sh
/opt/cad-atlas/venv/bin/python -m cad_agent.fleet.worker
```

两组 Worker 的环境独立，使用 `UMask=0007` 以支持共享缓存；服务定义通过 `EnvironmentFile` 加载相应配置。来源读取批处理保持关闭。模型服务的实际容量和两组可用性需要真实请求验证，进程健康不证明推理成功。

## 数据与执行语义

- 原始文件上传立即持久化。ODA 转换在后台串行进行，转换缓存按原文件指纹、ODA 服务及转换修订键区分。SDK 或转换设置变化时更新 `CAD_FLEET_CONVERTER_REV`。
- Worker 将原始 DWG、DXF、ODA JSON 和转换任务 ID 交给 agent；agent 使用现有完整校验链重新验证产物关系，不从 DXF 猜测 DWG 来源。
- 同一图纸可以被两组独立导入；中心记录节点、全局任务和本地快照/运行 ID。两组不会共享可写的 agent 数据库。
- 每次领取产生执行编号和租约。Worker 定时上报，丢失租约即停止继续分析并取消本地执行。旧执行不能覆盖重新分配后的结果。
- Worker 检查点保留运行 ID；提交分析使用同一执行编号幂等重试。进程重启后优先核对已有分析，不重复启动模型请求。
- 完成前归档回答、任务、运行日志和所有分页证据；证据全文验证 SHA256。失败或取消也可保留已有归档。中央 archive 中的完整记录不自动增加事实证据资格。
- 本地查询模式仅用于不调用模型的事实查询与连通性测试，应与真实模型验收分开记录。

## 独立沙盒

两组数据目录分别保存沙盒运行配置，复用固定版本的镜像。每次动态代码执行创建独立临时容器，禁网、只读根目录、无宿主目录挂载、非 root 用户，并设置内存、CPU、进程和超时限制。运行后清理容器。

沙盒隔离针对动态代码执行，不等同于 agent 主进程隔离。操作系统账户及目录权限用于分开 agent 数据；Docker 管理权限应按部署中的受控执行方式管理，不能向浏览器提供任意 Docker 命令接口。

## 日常检查

```sh
sudo systemctl status cad-atlas-center cad-atlas-agent-a cad-atlas-agent-b cad-atlas-worker-a cad-atlas-worker-b
sudo journalctl -u cad-atlas-worker-a -u cad-atlas-worker-b --since '30 minutes ago'
sudo /opt/cad-atlas/venv/bin/python /opt/cad-atlas/current/scripts/fleet_backup.py --check
```

检查网页任务列表中的运行状态、交付状态与缺口。遇到模型失败，先保留当前记录，再决定是否显式重试；不要把零输出或服务错误展示成“没有问题”。两组模型请求仍受服务端容量约束。

## 一致性备份

`fleet_backup.py` 必须以 root 运行。默认发现正在运行的中心任务或本地分析时返回 `deferred`，不停止服务。没有活动任务时先停止中心入口，再停止两组 Worker 和 agent；完成或异常时在 `finally` 中恢复原本正在运行的服务。ODA 和 nginx 不停止。期间网页可能短暂不可用。

备份包含全部 `/var/lib/cad-atlas`、`/etc/cad-atlas`、当前源码及清单；不包含 Python 虚拟环境、系统软件和 ODA。当前部署另外将 nginx/systemd/sudoers 配置副本保存于 `/etc/cad-atlas/deployment`，这些副本随配置目录备份；恢复脚本不会自动安装到系统目录。归档目录为 root 0700，压缩包 0600，含模型与内部服务密钥，只能在受控介质保存。遇到文件树中的符号链接或特殊文件时脚本拒绝备份，要求先核对实际存储布局，不会悄悄遗漏。

```sh
sudo /opt/cad-atlas/venv/bin/python /opt/cad-atlas/current/scripts/fleet_backup.py
```

默认写入 `/var/backups/cad-atlas`，首阶段不自动删除任何旧备份。管理员应检查磁盘空间，并另行建立受控异机备份。`--allow-interrupt` 仅用于明确允许中断活动任务的人工维护；定时任务不要使用此选项。

当前目标机已安装每日 03:30（Asia/Shanghai）的低峰定时任务，配置示例如下；脚本会在忙碌时跳过，需查看日志确认确实有近期成功备份。

```ini
# /etc/systemd/system/cad-atlas-backup.service
[Unit]
Description=Consistent CAD Atlas fleet backup

[Service]
Type=oneshot
User=root
UMask=0077
ExecStart=/opt/cad-atlas/venv/bin/python /opt/cad-atlas/current/scripts/fleet_backup.py
```

```ini
# /etc/systemd/system/cad-atlas-backup.timer
[Unit]
Description=Daily CAD Atlas fleet backup

[Timer]
OnCalendar=*-*-* 03:30:00 Asia/Shanghai
Persistent=true
RandomizedDelaySec=15m
Unit=cad-atlas-backup.service

[Install]
WantedBy=timers.target
```

```sh
sudo systemctl daemon-reload
sudo systemctl enable --now cad-atlas-backup.timer
sudo journalctl -u cad-atlas-backup.service
```

## 恢复与回滚

恢复脚本默认只检查归档，不改变服务或文件。它拒绝绝对路径、路径穿越、重复文件、链接及设备文件，并检查必要数据库、配置和源码。先执行：

```sh
sudo /opt/cad-atlas/venv/bin/python /opt/cad-atlas/current/scripts/fleet_restore.py /var/backups/cad-atlas/<备份文件>.tar.gz
```

确认清单与恢复目标后，才执行实际恢复：

```sh
sudo /opt/cad-atlas/venv/bin/python /opt/cad-atlas/current/scripts/fleet_restore.py /var/backups/cad-atlas/<备份文件>.tar.gz --apply
```

恢复期间停止上述五个服务，保留 ODA。脚本保留当前状态和配置为带时间戳的 `*.before-restore-*` 目录，将源码复制到新的 `restore-*` 发布目录并切换 `current`；恢复途中失败会尝试恢复原目录和原 `current`。全过程保留原服务账户 UID/GID，适用于同一台机器、账户未重建的恢复。

Python 虚拟环境不随备份替换。默认拒绝恢复与当前 `pyproject.toml` 不同的依赖声明；如需跨依赖版本恢复，必须先准备匹配的虚拟环境，再显式使用 `--allow-dependency-change`。恢复前需预留备份解包、旧数据保留及源码复制所需空间。不得自动删除旧数据。

恢复后核对五个服务、两个节点、历史任务、证据定位和一个明确的事实查询。旧备份中的运行租约可能已失效，原任务会按租约规则重分配或标记失败；不要宣称恢复能够续接已终止的模型计算。恢复失败导致服务重启失败时，脚本会返回错误，管理员需检查服务日志与保留目录。

同版本部署回滚可在维护窗口停止五个服务、将 `current` 切回上一个保留的发布目录，然后按 agent → center → worker 顺序启动；若同时存在数据库变更，应使用对应一致性备份恢复，不能仅切代码。

## 本次实机验证记录

部署验证记录保存于管理电脑的 `artifacts/cad-fleet-deploy-20261028/`。源码清单按文件 SHA256 冻结；部署基于原提交 `23664f6` 加本次工作区修改。启动检查、沙盒隔离、真实模型执行及回答质量分别记录，不以服务 active 代替模型验收。

ODA 曾因 SQLite 连接未关闭导致后台循环退出。本次仅修复连接释放与临时错误重试，原文件和哈希存于 `/home/e2e/oda-api/backups/fleet-20261028/`；SDK、转换器和授权配置未改动。
