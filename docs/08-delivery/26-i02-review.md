# 08.26 I-02：事实修订与人工复核

状态（2026-10-05）：在规则评估和规则配置批次之后，接通当前事实读取、人工事实完整快照修订、finding 查询以及 confirm/reject/cannot-determine 决策。实现仍只允许 dev/test，合成规则和 fixture 不代表真实化学安全规则已批准。

## 1. 本批交付

- `GET /inspection-items/{id}/facts` 返回当前 run 的事实；未建立 current pointer 时返回 `404`，读取只使用服务端租户/实验室授权。
- `POST /inspection-items/{id}/facts` 只接受 `needs_review` item 的完整 `entities/relations/dates` 快照。事务锁顺序为 item→run→当前 findings→evaluation，拒绝已确认/已派发/已关闭 finding；只允许当前 run 的 detection、image、crop 和固定 dictionary entity，所有事实来源必须是 `human`。
- 事实修订追加 `fact_revisions`，将旧 evaluation/findings 标记 superseded，创建新的 queued `rule_evaluation`、`task_runs` 和 `TaskDispatch` outbox，并更新 item/run current pointers；旧 lease、旧 generation 和旧 evaluation 不能复活当前结果。
- `GET /findings` 默认 `current_only=true`，支持实验室、状态和严重度过滤；finding projection 固定返回非空 `EvidenceRef[]`、task_id、superseded_at 和可信 `allowed_actions`。
- `POST /findings/{id}/confirm`、`reject`、`cannot-determine` 使用 domain guard、expected_version、review_actions、audit 和幂等响应；confirm 需要 evidence，不能对 superseded 或已处理 finding 重复操作。
- API 层使用严格 body/query allowlist；application 层负责身份、租户锁、幂等、死锁重试，持久层负责锁定完整写集和原子提交。

## 2. 验证

| 命令 | 结果 |
|---|---|
| `.venv-business\\Scripts\\python.exe -m pytest tests/business/test_review_boundary.py tests/business/test_rules_boundary.py -q` | 26 passed；1 个既有 Starlette/AnyIO 弃用警告 |
| `.venv-business\\Scripts\\ruff.exe check packages/persistence/review.py packages/application/review.py apps/api/app/review.py apps/api/app/main.py packages/persistence/foundations.py tests/business/test_review_boundary.py` | PASS |
| `APP_ENV=test .venv-business\\Scripts\\python.exe -m compileall -q apps/api/app packages/application packages/persistence` | PASS |
| API operation id smoke | 54 个 route，54 个唯一 operation id；新增 7 个复核 operation |
| `.venv-business\\Scripts\\python.exe -m pytest tests/business tests/domain tests/protocol -q` | 1477 passed；1 个既有 Starlette/AnyIO 弃用警告 |
| `tools/database/run_mysql_tests.py --mysqld <专用 mysqld>` | 386 passed；MySQL 8.0.33、43 表、115 外键、head=0001_initial；1 个既有弃用警告 |

本批真实 MySQL 使用独立临时 data directory、随机回环端口和专用 schema，测试结束后由 runner 清理；未连接或修改既有数据库。remediation、report/export、完整 item complete、真实存储部署验收和生产发布仍未完成。新增领域事件已写入 outbox，但通用领域事件 publisher 尚未接入，因此不宣称已实际发布。
