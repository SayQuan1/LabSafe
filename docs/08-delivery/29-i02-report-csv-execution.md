# I-02I2 CSV 报告持久执行与对象版本登记

状态（2026-10-06）：本批接续 I-02I1 的冻结快照，完成 CSV 报告的 dev/test 执行链路。默认关闭，未提交、Push 或创建 PR；不关闭完整 EXPORT-01 / JOB-04。

## 本批交付

- report→task 专属锁序；消息 tenant/task/resource/payload/generation/sequence 只用于核对持久记录。首次/重试仅领取 queued CSV，原子创建 attempt 并切换 report running/task leased。
- 60 秒数据库租约、独立 10 秒心跳、120 秒有界生成/上传子进程。外部工作不持数据库事务；完成后重新核对 report version、完整冻结输入和 owner/token/generation/attempt/有效租约。
- CSV 仅取冻结列/行，不查当前业务数据。UTF-8 BOM、CRLF、标准 CSV 引号转义；以 =、+、-、@、tab、CR 开头的文本加单引号。保留零 finding 行及 UTC 快照时间；输出上限 64 MiB，错误快照/超限为不可重试 SCHEMA_MISMATCH。
- 按排序最小 laboratory_id 构造规范 R 路径，文件名为实际 CSV SHA256。已有同 key 对象须读取精确版本并实算相同 SHA 才复用；不信任 ETag/声明 metadata，不同内容拒绝。PUT 必须返回有效、非 null VersionId。
- 围栏事务原子登记 object_key/checksum/object_version/size_bytes、ready、expires_at=DB now+24h、系统 actor 的 report.export_ready 审计以及 task/attempt succeeded；不冒充申请用户，不伪造 ReportReady 事件。
- DEPENDENCY_UNAVAILABLE/STAGE_TIMEOUT 按 5/30/120 秒加 0–20% 抖动重试；最多 4 attempts，耗尽进入 dead_letter/report failed。永久错误立即 failed。回收将旧 attempt 标 abandoned 并递增围栏；手工 replay 使用既有 API，保留快照和 attempt 历史。
- `WORKER_REPORT_EXPORT_ENABLED=1` 在 publisher/Worker 显式启用 CSV。独立 q.reports 避免未启用报告的 general Worker 消费报告通知。publisher 回收/补发均按库中 csv format 过滤；PDF 保持 queued/pending。
- 新迁移 `0002_report_object_version` 为 report_exports 追加两个 nullable 字段；原始迁移不变，生成源/设计契约/结构核对/readiness 同步。测试验证有旧快照的库升级后原字段不变。

## 验证

| 检查 | 本轮结果 |
|---|---|
| 业务/领域/安全/协议 | 1547 项全部通过，包含新增报告执行单元 33 项 |
| 报告执行与迁移定向验证 | 首轮修正结构检查器的旧 head 常量后，63 项通过（35 项报告 MySQL + 7 项结构检查 + 21 项持久化单元）；后续增加 2 项重放/旧上传围栏用例纳入完整回归 |
| 完整持久化回归 | 446 项全部通过（425 项真实 MySQL + 21 项持久化单元），包含报告执行 37 项及既有报告受理 21 项；MySQL 8.0.33、head=0002_report_object_version、43 表/115 外键 |
| 静态检查 | Ruff check、176 文件 format --check、git diff --check 通过 |
| 设计契约 | build_specs --check、148 项 readiness 设计检查、validate_specs 全部通过 |

报告专项覆盖并发领取、重复/旧/未来消息、8 类围栏或输入变化、4 次租约回收、10 个写入故障点回滚、事务外生成、CSV/PDF 发布门禁、失败重放和旧上传结果不能覆盖新结果。新增测试文件合计 70 项（33 单元 + 37 MySQL），不与上表重复累加。

真实 MySQL 验证使用 `tools/database/run_mysql_tests.py`，仅创建独立临时 data directory/schema 和随机回环端口；不连接既有数据库。S3 使用真实 SDK Stubber，不能替代实际 MinIO/IAM/TLS 验收。

完整回归命令（不设置 PYTEST_ADDOPTS 筛选）：

```powershell
.venv-business/Scripts/python.exe -m pytest tests/business tests/domain tests/security tests/protocol -q
.venv-business/Scripts/python.exe tools/database/run_mysql_tests.py --mysqld 'D:/SQL/MySQL/MySQL Server 8.0/bin/mysqld.exe'
```

完整持久化回归耗时 21 分 54 秒，无失败或跳过；仅有现有 Starlette/AnyIO 弃用警告。首次定向运行因结构检查器仍要求旧 head 导致夹具失败，已同步修正并完成上述重跑，未将失败轮算作通过。

## 启动、后续与回退

启动方式及 secret 配置见 [开发指南](../../DEVELOPMENT.md)。先迁移并 verify，再分别运行 publisher/sweeper/报告 Worker。已有旧版数据库需升级后才能通过 readiness。

后批仍需：PDF 所需授权证据/人工意见快照扩展和渲染；完成通知；ready 到 expired 的过期处理与精确对象版本清理；真实存储部署及 UI。ReportRequested 仍只是领域 outbox 记录。I-02I3 已补齐 CSV `downloadExport`：创建人或同范围 admin、全范围 export 权限、24h 内及 60 秒精确版本签名，见[报告下载验收](30-i02-report-download.md)。

停止报告 publisher 并排空消费者，再关闭报告开关；保留 schema、对象与任务/audit 历史。上传成功后 DB 未提交或旧租约丢失可能留下未引用版本，本批不删除它们。未来清理须查 DB 引用并按精确 key+version 执行，禁止按前缀递归删除；有数据时不以 downgrade 回退。
