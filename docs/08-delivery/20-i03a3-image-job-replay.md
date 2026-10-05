# I-03A3 图像任务查询与管理员重放

日期：2026-10-02。范围：在 A1/A2/F3 已实现的 validate_image 上接通 3 个既有公共契约；只供 dev/test。没有新增迁移、模型能力或配置项。其他任务类型、领域事件 publisher/inbox、真实存储部署与生产验收仍未完成。本批保留在 `wsq/i-03a-durable-dispatch`，未提交、Push 或创建 PR。

## 1. 接口与权限

| 方法与路径（前缀 /api/v1） | 契约 operationId | 权限与行为 |
|---|---|---|
| GET /jobs/{id} | getJob | 图像实验室 READ；仅 validate_image，其他类型/不可见资源 404 |
| GET /dead-letters | listDeadLetters | safety_admin；当前租户 validate_image 的 failed、dead_letter 技术终态；page/page_size/laboratory_id |
| POST /dead-letters/{id}/replay | replayJob | safety_admin；VersionCommand；202 返回 ready Job，异步执行 |

列表包括两类可人工重放的技术终态：failed 为永久技术错误，dead_letter 为自动重试耗尽。并非所有列出的任务都仍可重放：归属取消、输入状态变化或固定 staging 版本过期会在命令中返回 409。内容拒绝的任务为 succeeded，不进入此列表；修正图片后新建上传。

Job 投影严格使用公共字段：id/created_at/updated_at/version/task_type/resource_id/state/attempt/replay_generation/available_at/last_error_code。没有 tenant_id、payload、对象 key/version、lease owner、fencing token、原始错误或凭据。时间统一 UTC。列表总数与页面使用一个 RR 快照，created_at DESC、id DESC 稳定排序。

查询只需要身份服务配置；首次重放还需要 API S3 配置。HTTP 写入沿用同源 Origin、JSON、CSRF、Idempotency-Key；拒绝未知/重复 query 和非契约 body。expected_version 必须取 Job.version，不是 Image.version。配置与示例见 [开发指南 3.13](../../DEVELOPMENT.md)。

## 2. 实现入口

| 文件 | 职责 |
|---|---|
| [jobs.py](../../apps/api/app/jobs.py)（API） | UUID、查询参数、VersionCommand 和 202 HTTP 适配 |
| [jobs.py](../../packages/application/jobs.py)（application） | preflight/Redis 限流、两阶段重放、事务重验、幂等响应 |
| [job_replay.py](../../packages/domain/job_replay.py) | 严格命令、Admin、租户/类型/版本/终态/输入与计数容量守卫 |
| [jobs.py](../../packages/persistence/jobs.py)（persistence） | 白名单查询与 RR 分页；task/dispatch/审计原子写入 |
| [job_execution.py](../../packages/persistence/job_execution.py) | 与 claim/commit/recover 共用领域先锁的固定输入加载 |
| [s3.py](../../packages/storage/s3.py) | inspect_pinned_staging：HEAD 原 VersionId；metadata/错误校验 |

复用 A2 的可信 ImageInput，不允许客户端传 payload、generation、对象版本或替代输入。数据库原 payload/resource/logical_key 保持，准确对象 VersionId 从已登记 image 读取。

## 3. 重放执行顺序

1. 严格校验 body 的 expected_version/reason。短 preflight 检查当前身份、CSRF、Admin；Redis 写限流在 DB 事务外，失败返回 503，不能访问对象或修改任务。
2. 第一个短事务 locate session scope，锁 tenant 共享，建立临时幂等占位，再重新认证当前身份/Admin。已完成的相同 key/body 重验当前可见性后直接返回原始 202 body/request_id/version，不重新 HEAD、不检查已过时的任务终态或 expected_version。相同 key、不同 body 返回 409。
3. 新命令按 tenant→user/roles/session→owner→upload→image→task 加锁。巡检锁父再锁项；整改锁整改任务。验证 task.type=validate_image、版本匹配、state=failed/dead_letter；image/upload 仍 validating、归属允许采集且未取消/终结、lab/key/SHA/MIME/固定版本与 payload 一致。generation/dispatch_sequence/version 的 INT 容量不足返回 409。
4. 复制 ImageInput 后抛内部控制异常，使整个准备事务回滚，临时幂等占位和锁均释放。没有提交长期 pending，也没有持锁等待存储。
5. 事务外 HEAD 原 staging key+VersionId；校验返回版本、大小、MIME、DeleteMarker。没有 latest fallback、重新 grant、签名续期或生命周期延长。缺失固定版本 409 STATE_CONFLICT；存储访问故障/无权 503 DEPENDENCY_UNAVAILABLE；metadata 不匹配 409。HEAD 只验证当时可用，不证明实际 SHA 或解码成功。
6. 第二个短事务重复幂等、身份、CSRF、Admin、task.version/状态和领域输入检查。当前 ImageInput 必须等于准备阶段副本（含 image/upload version），否则 409。HEAD 期间撤权/会话撤销、owner 取消或输入变化不会提交；数据库死锁最多重新执行 3 次短事务，不重复存储 I/O。
7. 原子更新 task：generation+1、fencing_token+1、dispatch_sequence+1、attempt=0、state=ready、version+1；清 lease/heartbeat/started/finished/error，available_at=DB now、last_dispatched_at=now。last_dispatched_at 表示补发意图登记时间，不代表 broker ack。
8. 同事务写新的 schema 1.1 TaskDispatch、操作者 job.replay 审计、完整 202 幂等响应。任一步失败全部回滚。旧 attempts/原 payload/图片/上传不改；publisher/general Worker/sweeper 沿用 A1/A2/F3，新 generation 从 attempt=1 开始。

同 key 的并发请求允许收到缓存 202 或短暂 409 REQUEST_IN_PROGRESS；不同 key 使用同旧 version，最多一个提交，其余 409。已受理 202 不是执行成功证明，GET Job/Image 查询后续状态。HEAD 后原版本仍可能消失，由 F3 准确 GET、SHA/解码和围栏收敛，不能跨存储与 MySQL 声称原子保证。

## 4. 重放与失败处理

| 情况 | 结果与后续动作 |
|---|---|
| 非 Admin/实验室不可见/跨租户 | 403/404；会话失效 401；不访问对象、不创建 generation |
| Job.version 过时 | 409 VERSION_CONFLICT；重新 GET，确认是否仍需要重放，使用新 key |
| ready/retry_wait/leased/succeeded | 409 STATE_CONFLICT；不要用人工重放干预仍在运行的任务 |
| owner 已取消/终结或 image/upload 非 validating | 409；不复活领域状态；按业务入口处理 |
| 固定 staging 版本不存在 | 409；重新上传；24h 生命周期仍适用 |
| S3 未配置/不可达/无权或 Redis 失败 | 503；恢复依赖后同 key/body 可再次尝试，无遗留 pending |
| 结果写入步骤异常 | task/dispatch/audit/idempotency 全回滚；内部异常脱敏 500 INTERNAL_ERROR，DB 不可用 503 DEPENDENCY_UNAVAILABLE |
| 原 generation 消息、旧执行 lease | no-op/LEASE_LOST；无旧结果写回；新任务历史不被旧执行者覆盖 |
| 相同 key/body 已成功 | 原始响应；即使 Worker 已推进任务也不重复执行命令；当前身份/Admin 仍须有效 |

人工重放与自动重试区分：一次 generation 内仍最多 4 个 attempts；Admin 可在技术终态手动开启下一代，需要 reason 与审计。此接口不重建 inference run、替换 facts、广播通知或实现其他任务 replay。

## 5. 本地验证证据

业务边界见 [test_job_api.py](../../tests/business/test_job_api.py)，数据库/HTTP/Redis 见 [test_job_api_mysql.py](../../tests/persistence/test_job_api_mysql.py)。本批新增 44 项业务用例、39 项真实 MySQL 用例；对象 HEAD 使用真实 SDK Stubber，未连接真实 MinIO。

| 验证 | 2026-10-02 本地结果 |
|---|---|
| I-03A3 定向真实 MySQL | 39 passed，259.58s；无跳过；MySQL 8.0.33、43 表、115 外键、head=0001_initial |
| 业务/领域/安全/协议/持久化单元 | 1429 passed = 1404 主套件 + 4 协议 + 21 持久化单元，31.41s；1 项既有 Starlette/AnyIO 弃用警告 |
| 完整持久化回归 | 357 passed = 336 项真实 MySQL + 21 项单元，1084.41s；无跳过；1 项既有弃用警告；21 项单元与上行重叠 |
| 静态/格式/依赖 | Ruff check PASS；128 files format PASS；pip check PASS |
| 契约/设计/文档 | build_specs --check 16 制品未漂移；148 synthetic readiness；validate_specs PASS、262 文档链接 |

覆盖：Job/JobPage schema、全部状态投影、tenant/lab/角色隔离、Admin 两阶段重验、撤权后缓存拒绝、精确版本 HEAD/无 latest、HEAD 外无 task/image 锁与 pending、输入/owner 变化拒绝、四处写入失败全回滚、同/不同 key 并发只一代、原 attempts 保留、旧消息/lease 拒绝，新 generation 实际提交 F3 内容拒绝结果、无存储配置的重试与缓存响应、RR count/items 同快照。

### 可复现命令

~~~powershell
.\.venv-business\Scripts\python.exe -m pytest tests/business tests/domain tests/security tests/protocol tests/persistence/test_unit.py -q
$env:LABSAFE_TEST_REDIS_SERVER = '<专用 Redis 可执行文件路径>'
.\.venv-business\Scripts\python.exe tools/database/run_mysql_tests.py --mysqld '<专用 MySQL 8.x mysqld 路径>'
.\.venv-business\Scripts\python.exe -m ruff check apps packages tests tools/database
.\.venv-business\Scripts\python.exe -m ruff format --check apps packages tests tools/database
.\.venv-business\Scripts\python.exe -m pip check
python -B tools/design/build_specs.py --check
python -B tools/design/test_readiness_design.py
python -B tools/design/validate_specs.py
~~~

定向可临时设置 PYTEST_ADDOPTS='-x -k job_api'；完整回归必须移除该过滤变量。启动器只创建/清理自己的临时 MySQL/Redis，不触碰已有数据库或服务。完整持久化套件含 21 项与主套件重叠的单元，不相加宣称互不重叠。

本地代码与验证批次已关闭；远程 CI 和独立审批没有执行。AI 环境、模型和前端本批未修改，不以业务/数据库成绩替代其独立验收。

## 6. 回退与后续关闭项

停止 API 暂停新人工重放；已受理任务由 Worker 持续处理。如需暂停整条链路，按开发指南先停领取、预算内排空，再停 publisher/sweeper/general；保留任务、审计和全部历史，没有删除或降级迁移。

UP-03/SEC-03 仍需 versioned MinIO、真实 IAM、同源 HTTPS、实际 S3 生命周期与异常恢复验证，见 [F3 部署验收清单](19-i02f3-image-validation.md)。本批 Stubber 不能替代这些证据。其他类型的领域守卫/重放、事件 publisher/inbox、提交与 AI 闭环继续按实施计划完成；整个 I-02/I-03/JOB-04 未关闭，production 仍禁止。
