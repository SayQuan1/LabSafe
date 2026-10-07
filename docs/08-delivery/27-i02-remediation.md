# I-02H 整改任务与证据闭环验收

状态（2026-10-05）：本批代码和本地回归已完成，尚未提交、Push 或取得远程 CI/独立评审。report/export 不属于本批；生产环境仍禁止。

## 1. 交付范围

本批把确认风险接入可追踪的整改生命周期：

- `dispatchRemediation`：仅 confirmed finding 可派发，锁定实验室、受派人和 due_at；原子写入 task、finding 状态、review action、audit、outbox 和通知。
- `acceptRemediation`、`submitevidenceRemediation`：受派人按 task 版本认领并提交证据；图片必须是同一 remediation task 的 ready 图片。
- `recheckRemediation`、`rejectRemediation`：独立复查人使用最新 evidence；受派人和 evidence submitter 均被排除。
- `cannotremediateRemediation`、`reassignRemediation`：管理员无法整改收敛、具备 dispatch 权限的操作者转派回 pending_dispatch。
- `listRemediations`、`getRemediation`、`listEvidence`：租户/实验室授权后的白名单投影，`overdue` 为查询派生值，不落库伪造状态。

所有写命令复用 CSRF、租户锁、幂等 claim、死锁有限重试和版本围栏。事件只在事务内写入 outbox；本批不宣称 publisher 已实际发布。

## 2. 关键不变量

| 场景 | 验收要求 |
| --- | --- |
| 派发 | finding 必须为 confirmed；finding 更新和 task 插入不能部分提交；同 key 重放返回原响应且不重复通知/审计/outbox |
| 证据 | image tenant、laboratory、remediation_task_id 必须与 task 一致，状态必须为 ready；插入 evidence、绑定图片、latest pointer 和状态更新同事务 |
| 复查 | 只能检查 task 的 latest evidence；assignee 或提交人执行返回稳定 `FORBIDDEN`；recheck 原子关闭 task/finding，reject 回到 rejected/dispatched |
| 并发 | expected_version 过期返回 `VERSION_CONFLICT`；task/finding 的 rowcount 都必须命中，否则整笔回滚 |
| 隔离 | 跨租户和跨实验室资源在授权前隐藏；HTTP 不返回数据库异常详情 |

## 3. 验收证据

业务/领域/协议回归：

```text
.venv-business\Scripts\python.exe -m pytest tests/protocol tests/domain tests/business -q
1477 passed (baseline)

本批新增 API 边界 6 项通过，合计业务/领域/协议回归覆盖 1483 项。
```

完整持久化回归使用专用 runner，创建随机回环端口、临时 data directory 和 disposable schema；不会连接或修改已有 MySQL。整改专项用例为 `tests/persistence/test_remediation_mysql.py`，覆盖派发原子写集和幂等、accept/submit/recheck 状态链、ready 图片归属及 stale version。

```text
.venv-business\Scripts\python.exe tools/database/run_mysql_tests.py --mysqld "D:\SQL\MySQL\MySQL Server 8.0\bin\mysqld.exe"
```

完整持久化基线为 MySQL 8.0.33、`0001_initial`、43 表、115 外键，既有 386 项通过；本批整改专项在同版本隔离 runner 中新增 2 项并通过（合计覆盖 388 项，完整套件最后一次执行时新增用例尚未纳入，故不把两者合并宣称为一次 388 项全绿运行）。

## 4. 未关闭项

真实对象存储/IAM/TLS、领域事件 publisher 的实际投递、report/export、完整 UI 和生产发布门禁仍未关闭。整改 API 的完成不替代这些阶段门禁。
