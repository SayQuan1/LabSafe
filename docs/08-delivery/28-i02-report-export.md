# I-02I1 report/export 快照与任务受理

状态（2026-10-05）：本批实现创建导出、状态查询及 report_export 管理员重放基础；仅 dev/test。本地验收见下表，未提交、Push 或创建 PR。report/export 整体尚未完成，不能关闭 EXPORT-01 或 JOB-04。

## 已交付

- `POST /api/v1/reports/exports`：严格 ExportCreate/ExportFilter、CSRF、幂等及限流，成功返回 202。逐实验室检查 export 权限；lab_manager 可导出其已授权范围，viewer 动作不足 403，跨租户/不可见实验室 404。
- 31 天（含边界）、100 实验室、10000 条匹配的当前 finding 容量守卫；UTC 时间、重复 ID/筛选值拒绝；超量 422 提示缩小范围。日期按巡检创建时间筛选，from/to 两端包含；空 severity/status 表示不限。零 finding 巡检项仍保留一行，不占 finding 额度。
- 同一个 REPEATABLE READ 事务冻结字段及关联 ID：仅当前 run/fact revision/evaluation 且未 superseded 的 finding；以 laboratory→inspection→item→finding 排序。列遵循 API 文档 5.3；不包含用户身份或原图 GPS。count 与行数据共用事务一致性视图，后续业务变更不会重写快照。
- `report_exports.snapshot` 保存 schema_version、export_id、tenant_id、完整 laboratory_ids、snapshot_at、columns 和 rows。本批不新增表；文档中的 report snapshots 对应已有 JSON 列。
- 原子写入 queued 导出、ready report_export task、TaskDispatch、ReportRequested、审计和 202 幂等响应。相同 key 返回原响应；重放仍检查当前全范围 export 权限，并使用当前读避免并发请求拿旧 RR 视图误报 404。
- `GET /api/v1/reports/exports/{id}` 返回 ReportExport 白名单。对快照完整实验室范围重检 read 权限，包括没有匹配数据的实验室；不向客户端暴露 snapshot/object key。
- 既有 getJob/listDeadLetters/replayJob 扩展 report_export；死信列表的 laboratory_id 匹配完整 filters 范围。重放只接受 admin、正确 task.version、failed/dead_letter task 与 failed export，核对 export/task/payload/snapshot 绑定，按 report→task 加锁。
- 重放恢复 export queued；任务 generation、fencing_token、dispatch_sequence 递增，attempt 清零，清理旧 lease/错误/执行时间，保留原 snapshot/payload/旧 attempts，TaskDispatch/审计/幂等响应同事务。

## 验证

| 检查 | 本轮结果 |
|---|---|
| 业务、领域、协议 | 1500 项通过（含新增 17 项报告 HTTP/范围/消息守卫） |
| report/export MySQL 专项 | 最终 21 项通过（包含在下方 86 项内） |
| 既有完整持久化基线 | 首轮 388 项通过；该轮旧版新增测试夹具解包错误已修复，未将失败轮记为全量通过 |
| 最终受影响 MySQL 回归 | report_export、job API、dispatch 共 86 项全部通过，323 项未选择；不是全量 409 项通过 |
| 静态检查 | Ruff check、13 个相关文件 format --check、compileall、git diff --check 均通过 |

MySQL 8.0.33 通过 `tools/database/run_mysql_tests.py` 启动全新临时 data directory、随机回环端口及独立 schema；不连接既有数据库。最终定向运行使用：

```powershell
$env:PYTEST_ADDOPTS='-k "report_export or test_job_api_mysql or test_dispatch_mysql"'
.venv-business/Scripts/python.exe tools/database/run_mysql_tests.py --mysqld 'D:\SQL\MySQL\MySQL Server 8.0\bin\mysqld.exe'
```

真实测试覆盖当前关联/历史排除、零 finding 行、10000/10001 边界、实验室级权限、跨范围隐藏、原响应幂等/冲突/撤权、五类写入失败全回滚、错误版本/状态/绑定拒绝、failed 重放与旧 attempt 保留。

## 后续边界与回退

2026-10-06 续批：CSV 的发布、执行租约、编码、固定对象版本上传和 ready 登记已由 [I-02I2](29-i02-report-csv-execution.md) 接续实现；下述“下一批”描述为 I-02I1 当时边界，PDF/下载/通知等未实现部分仍保留。

本批仅登记任务意图。publisher 的任务类型白名单暂未开放 report_export，TaskDispatch 保持 pending，导出保持 queued，避免向尚不存在的消费者持续投递。ReportRequested 亦只写入 outbox，不宣称已实际投递。

下一批需接入 report 专属 lease/attempt/heartbeat/fencing、回收/失败收敛、CSV 公式防注入、PDF 分组及证据缩略图/人工意见快照扩展、对象版本登记和上传、ready/expiry、完成通知以及 downloadExport。当前没有可下载文件；CSV/PDF format 意图可受理，但不声明已生成。下载须满足创建人或同范围 admin、全范围 export 权限、24h 内及 60 秒签名；不能用未固定版本的对象 URL 提前实现。

回退恢复上一版程序并保留 report/task/outbox/audit/幂等历史；不清表、不删除对象或修改旧 attempt。真实存储/IAM/TLS、UI 与生产发布仍待独立验收。
