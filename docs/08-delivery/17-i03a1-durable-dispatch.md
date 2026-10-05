# 08.17 I-03A1：上传验证任务的持久调度

状态（2026-10-01）：基于已合并 PR #9 的 `origin/main`（070ed74），在 `wsq/i-03a-durable-dispatch` 本地完成并通过第 4 节验证；尚未提交或推送，不引用旧 PR 的 CI 作为本批证据。

## 1. 交付范围

I-03A 拆为调度与执行两批。本批 I-03A1 接通已存在的 `validate_image` 初始 TaskDispatch，交付 MySQL Outbox → Celery/Redis `q.general` 发布、发布租约回收和 ready/retry_wait 任务补发。公共 API 仍为 31 个，无新增依赖、表或迁移。

| 入口 | 本批行为 | 保留给后续 |
|---|---|---|
| publisher | 领取 Outbox、事务外发布、确认后登记 published | 其他 task_type 和领域事件路由 |
| sweeper | 回收过期 Outbox 租约；为到期待执行任务原子登记新 dispatch | 执行租约回收、attempt 重试与死信 |
| Worker | 配置 JSON、late ack、visibility_timeout=900；发布专用子进程 | `labsafe.tasks.dispatch` 消费处理器、领域结果事务 |

图像继续为 validating；task 为 ready 不是图像 ready。本批不实现 event_inbox、图像解码/真实 SHA/O/A 派生、人工 replay、推理或生产部署，不关闭整个 JOB-01/02/03/04。

## 2. 数据与锁语义

### 2.1 发布

`packages/persistence/dispatch.py` 只操作调度记录；`packages/application/dispatch.py` 负责事务边界；`apps/worker/dispatch.py` 提供两个独立进程入口。

1. 调度连接使用 **READ COMMITTED**，避免扫描索引更新时 RR 间隙锁阻塞；不改变 API 的 RR 一致性查询。所有时间条件和租约均来自 MySQL UTC_TIMESTAMP(3)。
2. 每轮最多处理 100 条，每次只领取 1 条到期 pending 的 validate_image TaskDispatch：`FOR UPDATE SKIP LOCKED`，owner 为新 UUID，lease=30 秒，attempts+1，提交事务。逐条领取避免批尾等待网络时租约耗尽。
3. 严格检查消息版本 1.1、字段白名单、UUID、trace、计数、带时区时间、payload 与 resource_id，以及外层租户/aggregate_id 一致性。暂未实现的 task_type/领域事件保持原记录，不交给 fixture。
4. 退出数据库事务后启动 spawn 发布子进程，发送 JSON Celery task `labsafe.tasks.dispatch` 至 `q.general`。Celery task_id 与 `outbox_event_id` header 均为稳定 Outbox.id；同事件重投保持该 ID，但 Celery ID 不被当作去重保证。
5. 发布预算 20 秒，10 秒仍在执行时用独立连接续约 30 秒；独立定时器到期终止发布子进程，不因数据库续约阻塞而放宽 broker 执行预算。退出路径 kill/join/close；主进程失败后由租约恢复，不能假设取消意味着 broker 未收消息。
6. 子进程确认发送后，重新锁行，再取 DB now，检查 owner/state/未过期 lease 后登记 published。旧 owner、过期 owner 或被接管 owner 不能续租、确认或退回 pending。网络/协议失败保留记录并延后 30 秒重试，仅记录 event_id，不输出异常正文、payload 或 Redis URL。

### 2.2 补发

每轮 sweeper 先用短事务回收最多 100 条过期发布租约，再用另一事务扫描最多 100 条到期 `ready/retry_wait` 的 validate_image 任务。`last_dispatched_at` 为空或距今至少 30 秒时，锁 task 后原子完成 sequence+1、更新时间、登记新 TaskDispatch。

`last_dispatched_at` 表示最近一次补发意图入库时间，不是 broker ack；F2 的初始值 NULL 允许首次 sweeper 立即补发。其后 30 秒内不会再次补发。原事件即使 published 也不阻止补发，因此 Redis 丢消息可以恢复。payload、generation 和资源 ID 取 MySQL，不取队列中的业务参数；重新调度创建新 event_id/trace_id，原有历史不修改。

终态、执行中或尚未到 available_at 的任务不补发。新 Outbox 插入失败时 sequence/version/last_dispatched_at 一起回滚。本批不改变 task attempt、fencing_token、业务 owner/image 状态；后续消费者必须按 generation/sequence/领域状态重新查库和领取，不能直接执行消息 payload。

## 3. 开发运行与回退

仅在专用 dev/test 数据库与可丢弃 Redis 上启用，默认 `WORKER_DISPATCH_ENABLED=0`，production 启动拒绝。数据库 secret/命名限制沿用开发指南 3.5/3.6，REDIS_URL 沿用业务环境；不要放入 Git 或共享日志。

~~~powershell
$env:APP_ENV = 'dev'
$env:WORKER_DISPATCH_ENABLED = '1'
# 沿用已配置的 DATABASE_URL_FILE、DATABASE_SCHEMA、REDIS_URL
.\.venv-business\Scripts\python.exe -m apps.worker.dispatch publisher --once
.\.venv-business\Scripts\python.exe -m apps.worker.dispatch sweeper --once
~~~

去掉 `--once` 后，在两个独立终端分别运行；publisher 每轮结束等待 1 秒，sweeper 等待 15 秒。日志输出发布、回收和补发数量；本批没有完整指标/告警平台。坏记录不删除，发布失败日志需人工调查；无效 task payload 会回滚该轮补发，须先修复可信数据来源后重试，不静默跳过坏数据。

尚无业务消费者，不要将当前 fixture Worker 手动订阅到 `q.general`，否则 Celery 会丢弃未注册任务。仅作调度验证，避免长期空转造成消息与 Outbox 积压。此限制不会因 published 状态消失，不能用该入口处理实际业务图片。

回退：停止两个调度进程并恢复开关为 0；保留 task/outbox/审计历史，不降级数据库、不清空 Redis 或业务表。正在发布的消息可能重复，后续处理器仍须提供持久围栏。

## 4. 验证记录

环境：Windows、Python 3.11.4、MySQL 8.0.33；数据库由仓库隔离启动器创建临时 datadir/随机端口/随机 schema，Redis 由测试夹具启动独立子进程，不使用现有业务服务。迁移 head=0001_initial，43 表/115 外键核对通过。

| 验证命令（业务 Python 环境） | 本轮结果 |
|---|---|
| `python -m pytest tests/business tests/domain tests/security -q` | 1290 passed，含本批 28 项；1 项既有 Starlette/AnyIO 弃用警告 |
| `python -m pytest tests/persistence/test_unit.py tests/protocol -q` | 25 passed（21 持久化单元 + 4 协议） |
| `PYTEST_ADDOPTS='-x -k dispatch_mysql'` 后运行隔离 MySQL 启动器 | 新增 26 项全部通过，213 deselected；包含真实 Redis/Celery 发送/丢消息/补发 |
| 完整 `python tools/database/run_mysql_tests.py --mysqld <installed-mysqld>` | 239 passed，1 既有 Starlette/AnyIO 弃用警告；MySQL 8.0.33、43 表/115 外键 |
| `python -m ruff check apps packages tests tools/database` | PASS |
| `python -m ruff format --check apps packages tests tools/database` | 111 文件通过 |
| `python -B tools/design/build_specs.py --check` | 16 生成物无漂移 |
| `python -B tools/design/test_readiness_design.py` | 148 项合成检查通过 |
| `python -B tools/design/validate_specs.py` | PASS，231 文档链接 |

本批新增 28 项业务测试和 26 项真实 MySQL 调度测试。完整持久化 239 项由 218 项真实 MySQL 用例和 21 项单元用例组成；后者与单独运行的 25 项套件重叠，不累加成唯一测试数。首次集成发现测试夹具缺租户 FK、RR 扫描间隙锁和 Redis 空队列断言差异，修正后完整回归通过。真实存储、业务消费者、独立 AI 和生产验收不包含在这些结果中。

故障覆盖：事务外发布、SKIP LOCKED 双 publisher、过期/错 owner/接管后回写拒绝、发布后未登记的重复发送、失败退避与日志脱敏、并发 sweeper 单次补发、插入失败原子回滚、未来/终态任务排除、真实 Redis 消息删除后从 published 记录恢复。

## 5. 下一批关闭清单

1. I-03A2：任务领取、attempt/执行租约/heartbeat/fencing；锁领域聚合后锁 task，当前版本与消息 generation/sequence 全部重验；旧执行者不能提交。
2. 同一领域收敛函数处理主动失败和过期执行租约，完成重试/4 次耗尽/永久错误/人工 replay，覆盖 JOB-04；不能只更新 task 状态。
3. 领域事件消费接入 event_inbox 和原子业务事务；扩展任务类型前补对应路由、入口与失败守卫。
4. I-02F3 接 general 图像验证、固定版本 GET/真实 SHA、解码和 O/A 生成，再原子登记 ready/rejected/ImageValidated。后续执行真实 IAM/存储/HTTPS 联调，关闭 SEC-03/UP-03。
