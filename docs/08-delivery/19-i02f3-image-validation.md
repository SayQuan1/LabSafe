# I-02F3 图像验证处理与原子结果

日期：2026-10-02。范围：在 I-02F2 上传受理、I-03A1 调度及 I-03A2 执行围栏上接通真实图像处理。代码仅供显式启用的 dev/test 环境；生产发布、真实 MinIO/IAM/HTTPS 验收和完整业务闭环仍未完成。本批没有新增公共 API、数据库迁移或模型推理能力，也未提交、Push 或创建 PR。

## 1. 实现入口与职责

| 文件 | 职责 |
|---|---|
| [image_validation.py](../../packages/domain/image_validation.py)（domain） | 成功/拒绝结果类型、内容错误白名单、O/A 规范 key、ready 证据约束 |
| [image_validation.py](../../packages/application/image_validation.py)（application） | 固定版本读取后的完整解码、归一化、事务外对象持久化；不接 DB connection |
| [s3.py](../../packages/storage/s3.py) | 准确版本 GET、字节 SHA、同 SHA 复用检查、固定源版本 COPY、分析图 PUT |
| [image_validation.py](../../packages/persistence/image_validation.py)（persistence） | 在持有领域锁/围栏的事务中登记图像、上传、归属、审计和成功事件 |
| [job_execution.py](../../packages/persistence/job_execution.py) | 提交前后核租约；ready 必须有合法 O/A key/version/SHA/尺寸；task/attempt 同事务终结 |
| [image_validation.py](../../apps/worker/image_validation.py)（worker） | 有界 spawn 处理子进程及私有消费入口；子进程不接 DB/broker handle |
| [main.py](../../apps/worker/app/main.py)、[run.py](../../apps/worker/run.py) | 默认关闭的 Celery 注册、q.general/solo/concurrency=1 启动入口、安全错误日志 |

启动、secret 配置、回退步骤见 [开发指南 3.12](../../DEVELOPMENT.md)。API/Worker/AI 均使用 Python 3.11；Pillow 11.3.0 加入业务 requirements/worker extra，不向独立 AI 环境添加 DB/S3/Redis 依赖。

## 2. 执行顺序

1. 严格校验 schema 1.1 的 validate_image 消息；消息只定位任务，输入来自 DB。其他 task_type、未来 generation/sequence 和额外字段不执行。
2. 通过 A2 短事务领取 lease，锁 tenant 共享→owner（巡检父/项或整改任务）→upload→image→task。固定 staging key/version、期望 SHA、字节大小/MIME、image/upload version。
3. 事务结束后启动独立心跳与处理子进程。子进程总预算 120 秒（含启动、GET、解码、O/A 检查与写入），父进程每 100ms 检查取消/预算；心跳每 10 秒用独立连接续租 60 秒。失败一经锁定不会因后续续租恢复而接受结果。
4. GET 必须带固定 VersionId，并复核响应版本、大小、MIME；流式读块 64KiB、最多 15 MiB，关闭 Body。实算 SHA 与声明值比较；不信 ETag/HEAD 自报 SHA，也不回退到最新 staging 版本。
5. 真实 Pillow verify 后重新打开并完整 load。仅 JPEG/PNG/WebP，真实格式必须与声明 MIME 一致；拒绝多帧动画、不可解码/截断、各边超过 10000 或总像素超过 40M。EXIF transpose 一次，转 RGB，使用新像素对象输出 PNG，去除 GPS/EXIF/ICC/text。分析图不缩放；检测/质量阶段的 resize 属于后续任务。
6. 用已校验 ID/SHA 生成规范 O/A key。已有对象先 HEAD 固定其准确版本，再 GET 实算 SHA；相同字节复用版本，异 hash/尺寸/MIME 冲突报 INTERNAL_ERROR，不覆盖。不存在才创建；原图 COPY 来源准确 staging 版本，A PUT 为 RGB PNG。写入必须返回有效非 null VersionId。分析 PNG 上限 128 MiB，容纳 40M RGB 像素的编码开销。
7. 子进程只返回结果证据（key/version/SHA/尺寸）或内容拒绝码，不返回原始图片字节。失租/超时终止并 join 子进程；失租抛 LEASE_LOST，超时进入 A2 技术失败恢复。
8. 停止准备阶段心跳后进入 A2 围栏提交。重检 DB 时间、owner/输入版本、owner 状态、lease owner/token/generation/attempt。结果、审计、成功 Outbox 与 task/attempt 终态在同一事务；任何一步失败全部回滚。commit 最后再次核 DB 时间，不让等待锁耗尽租约的执行者提交。

对象存储不参与 DB 原子事务。已写 O/A 但登记失败会留下孤儿；下一 attempt 实算同 SHA 后可复用。并发创建相同内容可能产生多个对象版本，只有当前围栏登记的准确版本成为证据。未登记版本留待受控清理，不授予 normal Worker 删除/列桶权限。

## 3. 结果与失败语义

| 情况 | 图像/上传与归属 | task/attempt | 审计/事件 |
|---|---|---|---|
| 成功 | ready，登记 O/A 准确 key/version、analysis SHA、尺寸；原始 SHA 保持已验证声明值 | 同事务 succeeded | service actor=NULL 的 image.validate；ImageValidated schema 1.1 |
| 内容失败 | rejected，清空分析证据；原图保持已固定 staging 引用以供历史定位 | succeeded，表示验证处理完成 | 审计记录错误码；不伪造缺少 analysis SHA 的 ImageValidated |
| 对象存储不可达/限流/无权；超时 | 保持 validating | A2 的 retry_wait，最多 4 次后 dead_letter | 本批不产生图像结果事件 |
| 内部错误/受控对象内容冲突 | 保持 validating | failed/INTERNAL_ERROR | 不覆盖冲突对象；不误判上传原图内容不合格 |
| 失租/owner 取消或终结/输入变化 | 拒绝结果登记；已有 O/A 仍为孤儿 | 由 A2 围栏/回收收敛 | 无新增图像结果、审计或事件 |
| DB 提交前失败/提交后重复消息 | 前者全回滚；后者 no-op | 当前记录保持唯一 | 不增加重复事件或审计 |

内容错误白名单：OBJECT_NOT_FOUND（固定 staging 版本不存在）、IMAGE_INVALID、IMAGE_TOO_LARGE、UNSUPPORTED_MEDIA_TYPE、HASH_MISMATCH。既有 O/A 读取过程的版本消失属于存储依赖故障，不以其取代 staging 或覆盖对象。

成功时 draft item→uploaded，其他允许采集态保持；inspection draft→in_progress。拒绝时按现行状态机，有其他 ready 图则 item=uploaded，否则 draft。整改图像只在 task in_progress/rejected 时接纳，验证不替代证据提交命令，task 状态保持。

ImageValidated 暂存 pending Outbox；A1 publisher 仅发布 TaskDispatch。领域事件 publisher/inbox/下游消费者未实现，不以事件已入库声称通知或推理已经执行。消费者捕获异常时只记录固定错误日志，DB 是重试/状态真相源；malformed 消息不会触发图像处理，不将 provider 原文、URL、图片或凭据交给 Celery 错误日志。

## 4. 本地验证证据

业务/图像用例见 [test_image_validation.py](../../tests/business/test_image_validation.py)，真实数据库用例见 [test_image_validation_mysql.py](../../tests/persistence/test_image_validation_mysql.py)。全部图片在内存中生成，不含真实实验室数据。

本批新增 49 项业务用例、37 项真实 MySQL 用例。不得把 skip、Stubber 或 synthetic storage version 当作真实存储部署证据。

| 验证 | 2026-10-02 本地结果 |
|---|---|
| 业务/领域/安全/协议/持久化单元 | 1385 passed = 1360 主套件 + 4 协议 + 21 持久化单元；1 项既有 Starlette/AnyIO 弃用警告 |
| I-02F3 真实 MySQL 定向回归 | 37 passed；无跳过；包含 Redis/Celery solo Worker |
| 完整持久化回归 | 318 passed = 297 项真实 MySQL + 21 项单元；1 项既有弃用警告，862.11s；21 项单元与上行重叠，不能相加宣称互不重叠 |
| 静态/格式/依赖 | Ruff check PASS；format 122 files PASS；pip check PASS |
| 契约/设计/文档 | build_specs --check 16 制品未漂移；148 synthetic readiness；validate_specs PASS、248 文档链接 |
| 独立 AI 环境 | 15 项 fixture + 4 项协议 PASS；业务/数据库/S3/模型依赖隔离 PASS |

本地通过与远程 CI、独立审批分开记录；本轮未创建 PR，不沿用 #9 的 CI 成绩。

- 真实 Pillow：JPEG/PNG/WebP、灰度/调色板/16-bit/带 alpha PNG、EXIF 一次旋转、元数据剥离、动画拒绝、格式伪装/截断及尺寸边界。
- 真实 SDK Stubber：准确 GET/CopySource VersionId、精确写入参数与版本、既有对象实算 SHA 复用、错误 metadata 不欺骗复用、冲突/存储错误无覆盖。
- 真实 spawn：Pillow 处理结果跨进程返回，卡住的子进程超时/取消后 kill+join，无遗留子进程；存储版本在该用例中为 synthetic。
- 真实 MySQL 8.0.33：完整 ready/rejected、巡检/整改归属、合法事件 schema、服务审计、六个提交步骤失败回滚、失租/取消/输入版本变化、禁止裸 ready、并发重复领取只写一次。
- 真实 Redis/Celery solo Worker：消费 TaskDispatch 并提交 rejected 结果；图像准备使用 synthetic handler，不证明真实 MinIO 链路。另有真实解码→MySQL ready、对象适配 Stubber 分层证据。

### 可复现命令

~~~powershell
.\.venv-business\Scripts\python.exe -m pytest tests/business tests/domain tests/security tests/protocol tests/persistence/test_unit.py -q
.\.venv-business\Scripts\python.exe tools/database/run_mysql_tests.py --mysqld '<专用 MySQL 8.x mysqld 路径>'
.\.venv-business\Scripts\python.exe -m ruff check apps packages tests tools/database
.\.venv-business\Scripts\python.exe -m ruff format --check apps packages tests tools/database
.\.venv-business\Scripts\python.exe -m pip check
python -B tools/design/build_specs.py --check
python -B tools/design/test_readiness_design.py
python -B tools/design/validate_specs.py
~~~

数据库启动器创建新临时目录、随机 loopback 端口和专用 schema，仅清理自己创建的实例。需要 `LABSAFE_TEST_REDIS_SERVER` 指向 Redis 可执行文件；CI 沿用已配置的可丢弃服务。

## 5. 尚待关闭的部署验收

| 关闭项 | 执行动作与通过条件 |
|---|---|
| UP-03/SEC-03 真实对象链路 | 专用 versioned MinIO + nginx 同源 HTTPS；用 API grant PUT→complete 202→publisher/general/sweeper→GET image ready，用 SDK GET 登记的准确 O/A 版本比对实算 SHA、方向与元数据 |
| 凭据隔离 | API 凭据不能 PUT O/A；general 凭据不能 Delete/ListBucket/管理、跨授权前缀访问；浏览器不能任意 GET O/A；记录实际拒绝结果 |
| 版本与故障 | staging 重复 PUT 后只处理 pinned version；缺失版本 rejected；写 O 后断存储保持 validating；恢复重试同 SHA 复用；DB 登记前 kill 后旧结果拒绝 |
| 生命周期/孤儿 | S 当前/非当前版本 24h 清理实测；业务 O/A 不受该策略影响。受控孤儿清理按精确版本核 DB 引用，独立 cleanup 凭据和任务尚待实现 |
| 生产资源/发布 | 40M 高熵图的实际耗时、峰值内存和 120s 预算测量；容器资源、进程关闭、凭据范围和完整系统门禁验收后才允许 production |

以上是实现后的验收事项，不重新打开模型选型或重做领域设计。当前关闭本地代码批次，继续后续提交/推理业务入口和 I-03 其他任务适配；生产和真实存储门禁保持未完成。
