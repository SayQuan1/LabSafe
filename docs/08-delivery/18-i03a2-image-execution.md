# 08.18 I-03A2：上传验证任务执行基础

状态（2026-10-01）：继续在 `wsq/i-03a-durable-dispatch` 上实现，保留上一批未提交改动。本批范围为 validate_image 的执行租约、围栏、技术失败重试与过期回收；验证见第 4 节。尚未提交/Push/PR，I-03A2 的其他任务类型及人工 replay 集成仍未完成。

## 1. 交付与边界

| 层 | 文件 | 责任 |
|---|---|---|
| 领域值 | `packages/domain/job_execution.py` | 不可变 ImageInput/ImageLease、租约异常、技术错误白名单、5/30/120 秒加 0–20% 抖动 |
| 持久化 | `packages/persistence/job_execution.py` | 锁聚合后锁 task、领取/attempt、续租、围栏提交、失败/回收原子写入 |
| 应用 | `packages/application/job_execution.py` | 独立短事务、prepare 外部工作、后台心跳、结果事务、限量回收 |
| 进程 | `apps/worker/dispatch.py` | 既有 sweeper 增加过期执行租约回收，再执行 A1 发布回收/补发 |

无新增公共 API、迁移、依赖或配置项。没有注册 `labsafe.tasks.dispatch` Celery 消费任务，fixture Worker 仍不得订阅 q.general；仅补齐未来真实验证处理器可调用的服务。测试中的 rejected handler 是合成事务夹具，不能冒充 SHA/解码/图像 ready 能力。

## 2. 开发者接入契约

### 2.1 消息与领取

调用 `ImageExecution(engine).execute(message, prepare, write_result)`；实例仅在 APP_ENV=dev/test 下创建。API 与普通 Worker 启动不自动创建或领取任务。message 必须符合已实现的 validate_image 消息 1.1 分支；不接受其他 task_type。

查库只用 tenant_id/task_id。数据库记录决定资源、payload、attempt、generation 和输入；消息不会覆盖它们。旧 generation 直接不执行；generation 相同才比较 sequence，旧 sequence 不执行，超前 generation/sequence 拒绝。不存在、终态、执行中、未到 available_at 均不重复执行。资源和 payload 不一致拒绝，不将消息字段传给存储操作。

事务使用独立 READ COMMITTED 连接，锁序如下：

1. tenant 共享锁；非 active 租户不能开始/继续执行。
2. locator 普通读定位资源，不先锁 task。
3. 巡检图片锁 inspection → inspection_item；整改图片锁 remediation_task。
4. 锁 upload → image → task_run，重新核对引用、租户/实验室、状态与 task payload。

巡检 owner 仅允许 draft/uploaded/needs_retake/needs_review/failed，父巡检必须 draft/in_progress；整改仅允许 in_progress/rejected。upload/image 均须 validating；key 必须是登记的 staging key，声明 SHA/MIME/大小一致，精确 object_version 不得空或 `null`。grant 到期不影响已经受理的任务，Worker 不重新签发 grant。

领取成功在同事务将 attempt+1、fencing_token+1、state=leased、owner=新 UUID、lease=DB now+60 秒，并插入唯一 running task_attempt。插入失败整体回滚。ImageLease 保存 DB 输入、upload/image version 与租约身份；不携带队列提供的对象路径。此阶段不改 image/owner 业务状态。

### 2.2 外部工作、心跳与结果事务

`prepare(input, cancelled)` 在无数据库事务状态执行，只收到不可变 ImageInput 和取消 Event。后续处理器必须执行固定版本 GET/SHA/解码，遵循有界 I/O，并检查取消信号；本批没有外部图像工作硬超时或强制终止实现，不应接入无界/阻塞不响应取消的处理器。

独立 heartbeat 线程每 10 秒另开数据库连接，按同一锁序检查 owner/token/generation/attempt、未过期 lease、owner 当前可采集状态、固定输入和 upload/image version，成功延长 60 秒。可变 owner 按当前状态重验，不能因另张图片将 draft 推进 uploaded 而拒绝合法输入；upload/image 本身在同一 attempt 内必须保持原版本。

续租返回 false 或数据库异常均锁定本次 attempt 的失败标记，发出取消信号；即使随后数据库恢复，`execute` 也不再调用 commit。线程无法在退出时 2 秒内收敛也禁止提交，等待数据库操作结束及租约回收。心跳日志只记 task_id，不输出驱动异常、URL 或输入。`LeaseHeartbeat` 是外部工作包围器，不在结果事务内等待心跳线程。

`write_result(connection, input, outcome)` 是服务端受信任的事务回调，不能来自 HTTP/队列。进入前重新锁聚合和 task，核 owner/token/generation/attempt/未过期 lease 及原输入版本。回调负责 image/upload、owner、审计和应有事件的**完整**写集，不得做对象/网络调用。

回调返回后要求 image 为 ready/rejected 且 upload 状态相同，再以新 DB now 重验租约；成功才将 attempt/task 同事务标 succeeded，清空 lease。内容不合格而正确登记 rejected 也是“验证任务成功完成”，不是技术失败。回调抛错、未落终态、提交前租约到期、attempt 更新失败都会整体回滚。重复消息不会再生成结果。

该基础只检查终态与租约，不替代 F3 对 ready 对象证据、owner 迁移、审计和 ImageValidated 的完整校验。不能传入只改 status 的业务实现；当前唯一此类 handler 位于测试夹具中。

### 2.3 技术失败与过期回收

目前 `fail` 只接受契约中的 DEPENDENCY_UNAVAILABLE、STAGE_TIMEOUT（可重试）和 INTERNAL_ERROR（不可重试）。IMAGE_INVALID/HASH_MISMATCH 等必须由验证处理器形成拒绝结果，不能借该 helper 跳过业务状态事务。

| 情况 | task | attempt | 图像/owner |
|---|---|---|---|
| 当前租约技术可重试失败，attempt 1–3 | retry_wait，available_at=DB now+退避 | failed | 保留 validating/现有 owner |
| 第 4 次可重试失败 | dead_letter | failed | 保留输入，等待人工处理 |
| INTERNAL_ERROR | failed | failed | 保留输入，不伪造图像无效 |
| 已过期租约，attempt 1–3 / 第 4 次 | retry_wait / dead_letter | abandoned | 保留输入 |
| owner 已取消/终结/不可采集或输入无效 | failed/LEASE_LOST | 已领取 attempt 标 failed；回收时 abandoned | 不复活或覆盖当前业务状态 |
| 旧 owner/token/generation 或普通失败请求已过期 | 不变 | 不变 | 不变 |

失败和回收共用收敛函数，同事务更新 task/attempt 并 token+1；回收者不依赖旧 Worker。sweeper 每轮最多发现 100 个过期任务，再逐个按领域优先锁序重验 token/generation/过期条件，避免反向先锁 task；两个 sweeper 竞争时仅一个成功。回收后沿用 A1 的到期补发，Redis 仍不是状态源。

技术失败不生成 ImageValidated：该事件契约要求有效 analysis_sha256，不能为网络失败伪造字段。没有人工 replay API，也不自动重置 generation/attempt；管理员后续需要通过带权限、版本、输入可用性重验、审计和幂等的命令恢复。

## 3. 运行与回退

沿用开发指南 3.11、WORKER_DISPATCH_ENABLED 和数据库/Redis 配置，无新增启用命令。sweeper 日志增加 `execution_recovered`；先回收执行租约，再执行发布租约回收与补发。尚无验证消费者，默认情况下无新增 execution lease 是预期。

回退停止调度进程、关闭开关并恢复上一版程序；保留 task/attempt/outbox 及业务数据。任何遗留 leased 等待后续正确版本的 sweeper 回收，不能清表、手动伪造成功或将失效 lease 续上。生产仍禁止。

## 4. 验证

新增 21 项单元测试、42 项真实 MySQL 测试。数据库为隔离启动器创建的 MySQL 8.0.33 临时 schema，head=0001_initial，43 表/115 外键无漂移；未使用已有业务数据库。

| 实际命令（业务 Python 3.11.4） | 本轮结果 |
|---|---|
| `python -m pytest tests/business tests/domain tests/security -q` | 1311 passed，含新增 21 项；1 项既有 Starlette/AnyIO 弃用警告 |
| `python -m pytest tests/persistence/test_unit.py tests/protocol -q` | 25 passed（21 持久化单元 + 4 协议） |
| `PYTEST_ADDOPTS='-x -k job_execution_mysql'` 后运行隔离 MySQL 启动器 | 42 passed，239 deselected |
| 完整 `python tools/database/run_mysql_tests.py --mysqld <installed-mysqld>` | 281 passed，1 既有 Starlette/AnyIO 弃用警告；MySQL 8.0.33、43 表/115 外键 |
| `python -m ruff check apps packages tests tools/database` | PASS |
| `python -m ruff format --check apps packages tests tools/database` | 116 文件通过 |
| `python -B tools/design/build_specs.py --check` | 16 生成物无漂移 |
| `python -B tools/design/test_readiness_design.py` | 148 项合成检查通过 |
| `python -B tools/design/validate_specs.py` | PASS，234 文档链接 |

各套件分开计数，持久化单元用例与完整持久化套件重叠，不重复累加。整改 owner 的首轮失败来自通用 Graph 夹具预先添加的 run_images 引用；改为尚未被 run 选用的采集阶段夹具后，42 项数据库测试全部通过。另补了心跳取消导致外部异常时禁止旧执行者写失败状态的单元回归。

覆盖重复领取、消息版本/身份/未来和过期调度、attempt 插入回滚、旧租约续租/失败/提交拒绝、固定对象版本变化、取消和 busy owner、整改 owner、双 sweeper、两种四次耗尽、迟到结果、回调/attempt 失败回滚、事务外工作及独立心跳失效。迁移和采集接口回归仍须完整运行；模拟 handler 不算真实图像或模型验收。

## 5. 后续关闭清单

1. I-02F3：实现有界的固定版本下载/实算 SHA/解码/方向归一/EXIF 清理/O/A 派生，将完整结果写集接入本批 `execute`，再注册 general 消费任务并验证 late ack/reject-on-loss。当前不应开启 consumer。
2. I-03A2 剩余：按 inference/rule/report 各自领域入口接入专属失败收敛，覆盖 JOB-04；不能把本批 image guard 直接用于其他任务。
3. 人工 replayJob：权限、幂等、expected_version、制品/精确输入仍存在、generation 与调度序号递增、审计及旧 attempt 历史；当前不可通过改数据库标 ready 替代。
4. event_inbox/领域事件消费、真实存储 IAM/HTTPS、独立 AI 与最终业务闭环仍按各自阶段验收。
