# 02.5 持久任务、Outbox 与崩溃恢复

## 1. 唯一真相源

MySQL task_runs/outbox_events 为任务和事件真相源；Redis 仅承载通知式队列，丢 Redis 不丢业务任务。保证至少一次调度/计算、至多一个当前业务结果提交；不保证 exactly-once 推理。message 的 task_id/generation/dispatch_sequence 只用于查库，不能由消息覆盖 DB payload、attempt 或权限。

输入协议：[任务](../../contracts/task-message-v1.json)、[事件](../../contracts/events-v1.json)。未知版本或类型不得执行，写隔离日志和告警；禁止反序列化 pickle，Celery accept_content=['json']、task_serializer='json'。

## 2. API 提交和 Publisher

领域提交在同事务写聚合、task_runs(state=ready,attempt=0,generation=0,dispatch_sequence=1)、业务事件 Outbox。logical_key 是 task_type:resource_id（规则任务用 evaluation_id）。事务外不能“先发队列再落库”。

Publisher 每 1 秒扫描最多100条到期 pending，使用 SELECT FOR UPDATE SKIP LOCKED；设置 lease_owner=本批UUID、lease_until=DB now+30s、state=leased 后提交。事务外发布 event_id 稳定的消息；broker 确认后 CAS(owner,state=leased)→published。中途崩溃可重复发布。租约过期行回 pending；publisher每10秒续约，发布最长20秒。

消费领域事件需在同一事务插入 event_inbox(event_id,consumer)，并创建/更新它负责的任务；唯一冲突意味着已成功处理。notify 的 recipient 按事件发生时 DB 权限计算并写 task payload，重放不得广播给任意新用户。

## 3. Worker 领取与围栏

1. 读取消息，用 tenant/task_id 查 DB；generation 或 dispatch_sequence 落后直接 ack；超前拒绝并告警；终态或未到 available_at 也 ack。
2. 锁相应领域聚合，再锁task_run；只可从ready/retry_wait领取，且按task_type检查领域入口。inference_pipeline要求当前item queued，ready对应run queued、retry_wait对应run retrying；rule_evaluation要求当前fact/evaluation匹配且item processing，禁止套用inference的item queued守卫。数据库now作为唯一租约时间。
3. attempt+1、fencing_token+1；记录唯一 task_attempt；state=leased、lease_owner=worker执行UUID、lease_until=now+60s、heartbeat=now；提交。
4. 独立 heartbeat 每10秒用 owner+token 条件延长60秒；数据库不可达时不得继续提交结果。一次续租失败立即尝试取消 RPC；允许 AI 继续计算，但其结果不会被接纳。
5. 执行外部工作；随后锁聚合再锁任务，核 owner/token/lease>now、generation以及当前领域版本。全部成立才提交业务结果、审计、Outbox、task succeeded、attempt succeeded。
6. 事务提交后 ack。若 ack 丢失，重投看 DB 已终态直接 ack，不能再次生成 findings/通知。

Celery acks_late=true、task_reject_on_worker_lost=true、worker_prefetch_multiplier=1、result backend 不作状态源；Redis visibility_timeout=900s。AI 调用进程外，heartbeat 不能依赖被计算阻塞的线程。关闭 worker 先停领取，再在 hard limit 内排空；被 kill 由 sweeper 收敛。

## 4. 超时回收、重试与死信

Sweeper 每15秒扫描过期 leased：按同样锁序 CAS 当前 token，token+1 使旧执行者失效；attempt 标 abandoned。attempt<4 →retry_wait，否则 dead_letter。retry delay 为第1/2/3次失败后 5/30/120秒，加均匀随机0–20%抖动；测试用固定种子。永久错误立即 failed，不浪费全部4次。

IRR-01：主动可重试失败和sweeper回收使用同一领域收敛函数，并与task/attempt状态同事务提交。当前inference未耗尽：run→retrying、stage→queued、item→queued；耗尽或永久错误：run→failed、stage→done、item→failed。当前rule_evaluation未耗尽：evaluation→queued、item保持processing；耗尽/永久错误：evaluation→failed、item→failed，保留facts。若run/fact已替代、inspection已取消，则只将旧task标failed/LEASE_LOST并失效围栏，不修改当前item/run/fact；不会为了重试复活旧领域状态。正常失败路径检查未过期owner/token；sweeper仅可回收已过期lease，先token+1。

| inference入口 | 领取前task / run / item | 原子领取后 | 执行起点 |
|---|---|---|---|
| 首次或人工replay | ready / queued / queued | leased / processing / quality_checking | quality；stage=quality |
| 主动失败后自动重试 | retry_wait / retrying / queued | leased / processing / quality_checking | quality；不续跑半截OCR/规则 |
| 超时、进程kill后租约回收 | retry_wait / retrying / queued | leased / processing / quality_checking | 同上；原attempt abandoned |

重跑仅复用不可变输入与固定版本，未提交crop作为孤儿处理；不写部分facts/findings。人工retry创建新run与同run自动重试仍是两种命令。JOB-04覆盖以上入口以及已替代/取消/旧token拒绝，不仅检查task_runs枚举。

可重试性以 error-codes.json 为准。失败写 task 和 run 状态必须仍持有合法 token；旧 worker 不得把新 worker 的任务写成 failed。数据库完全不可用时只记录脱敏日志，等待租约回收，不能用 Redis 写“成功”。

Sweeper 同时扫描 ready/retry_wait 且 available_at≤now、last_dispatched_at 空或超过30秒的任务：锁行，dispatch_sequence+1，写专用调度 Outbox，再提交。消息丢失或 Redis 清空均能重新发出；不得因已 published 的初始事件就停止扫描。Outbox 同时承载 domain event 和 task dispatch，两者 payload 分别严格符合 events-v1/task-message-v1，event_type 调度时固定 TaskDispatch。

replayJob 仅 admin，expected_version 匹配且 state=dead_letter/failed。先验证源领域仍是当前、未取消/未替代及固定制品仍存在；generation+1、attempt=0、token+1、dispatch_sequence+1、state=ready、保留旧 attempts、写审计。若 inference run 已被替代，返回 STATE_CONFLICT；用户应在 item retry 创建新 run。人工重放不得无限自动执行，也不得重放不可逆外部通知到不同收件人。

重放同时恢复可执行领域状态：inference_pipeline使当前failed run→queued、item→queued；rule_evaluation使对应evaluation→queued、item→processing，但不删除facts或改当前run；report_export使failed→queued。其他任务保持其已登记资源/原payload。不存在相应失败状态时409；旧终态attempt日志不得改写。

## 5. 各任务原子结果

| task_type | resource_id / logical_key | 成功提交 |
|---|---|---|
| validate_image | image_id | image ready/rejected、owner 状态、ImageValidated |
| inference_pipeline | run_id | 同事务事实revision1、完成规则evaluation、候选、run/item、InferenceCompleted/RuleEvaluationCompleted |
| rule_evaluation | evaluation_id | 对当前fact修订评估、候选、item和当前evaluation |
| report_export | export_id | 已上传文件key/hash、ready/expiry |
| notification_create | event_id:recipient_id 组成 logical_key；resource_id=event_id | notification 唯一(event,recipient)，不发外部邮件 |
| overdue_scan | 由日期+lab生成稳定resource_id | 对到期task按(任务,到期日)幂等生成通知事件 |
| object_cleanup | deletion_request_id | 再核无引用/legal_hold，删除指定key+version，记审计 |

所有对象写入采用确定 key + checksum 后登记；重试发现同hash复用，异hash报错。孤儿对象清理必须以 DB 引用核验为准，不按目录粗暴删除。

I-02I2 的 CSV report_export 使用 report→task 锁序、60 秒租约与独立 10 秒心跳；无 DB 句柄的生成/上传子进程最长 120 秒。提交核对冻结输入及聚合版本、owner/token/generation/attempt/租约有效期，原子登记 key/checksum/object_version/size_bytes、ready、DB now+24h 的 expires_at、系统审计和 task/attempt succeeded。可重试错误或过期回收恢复 report queued；第 4 次耗尽或永久错误置 failed，旧 attempts 保留。报告采用 dev/test opt-in q.reports，发布/补发按 DB format=csv 筛选，PDF 仍 pending。I-02I3 的 downloadExport 在当前租户/报告锁内重新核验创建人或同范围 admin、完整 export 权限、ready/24h 门禁和精确 key+VersionId，再在事务内只做本地 60 秒签名；不重生成、不读取对象。完成通知、过期处理仍待后批，详见 [I-02I2](../08-delivery/29-i02-report-csv-execution.md) 与 [I-02I3](../08-delivery/30-i02-report-download.md)。

## 6. 必测故障点

提交前kill→无业务/无事件；提交后发布前kill→sweeper补发；发布后mark前kill→重复事件由inbox防重；AI完成后DB提交前kill→可重复计算但仅当前token提交；DB提交后ack前kill→重复消息无新增结果；lease过期后旧结果→拒绝；Redis清空→30秒扫描+发布恢复；超过4次→dead_letter；手工重放→新generation且完整历史。

## 7. 当前实现边界

I-03A1 接 validate_image TaskDispatch 的 publisher、发布租约回收与待执行任务补发，采用独立 READ COMMITTED 调度连接、逐条领取（每轮最多 100）和事务外有界发布；API 的 RR 快照不变。`last_dispatched_at` 为补发意图入库时间，不是 broker ack，见 [I-03A1 验收](../08-delivery/17-i03a1-durable-dispatch.md)。

I-03A2 补 validate_image 执行租约、attempt、独立心跳、围栏提交和技术失败/过期回收。锁 tenant 共享→owner→upload→image→task；技术失败不伪造 rejected。精确接入契约见 [I-03A2 验收](../08-delivery/18-i03a2-image-execution.md)。I-02F3 已接 opt-in general 消费、120 秒有界图像处理子进程及完整结果事务；默认 solo/concurrency=1，10 秒续租不依赖解码线程。内容拒绝不发缺少 analysis SHA 的 ImageValidated，成功事件写 Outbox，见 [I-02F3 验收](../08-delivery/19-i02f3-image-validation.md)。

I-03A3 接 getJob/listDeadLetters/replayJob 的 validate_image 分支。详情按图像实验室 READ；列表/重放仅 Admin，列表收 failed/dead_letter 两类技术终态。重放先短事务鉴权和加载固定输入，回滚临时幂等占位并释放锁，事务外 HEAD 原 staging VersionId，再短事务重验 Admin/归属/输入版本/task.version；generation、token、dispatch_sequence 增一，attempt 清零，原 payload/旧 attempts 保留，TaskDispatch/审计/202 幂等响应同事务。HEAD 只证明固定输入当时可读，SHA/解码仍交 F3；版本缺失 409，不回退最新版本。cached replay 也重验当前 Admin。精确故障证据见 [I-03A3 验收](../08-delivery/20-i03a3-image-job-replay.md)。其他领域事件发布/inbox、任务适配及其 replay 仍未接通；不能据此关闭整个 JOB-04 或业务闭环。

I-02G1 已将 `inference_pipeline` 按既有 task-message-v1 分支写入持久任务和 TaskDispatch outbox。I-03A2 现已补齐 opt-in 推理消费者：按 item→run→task 专属锁序领取、独立心跳、attempt/fencing、quality/runs 协议校验、`facts_ready` 首个 fact revision 围栏提交、规则任务入队及过期回收；详见[推理执行验收](../08-delivery/23-i03a2-inference-execution.md)。`rule_evaluation` 再按 item→fact/evaluation→task 锁序三值求值，原子生成 evaluation/findings 并收敛到人工复核，详见[规则评估验收](../08-delivery/24-i03a2-rule-evaluation.md)。rules 管理 API、report 下游、D-FINE-N/CUDA 真实执行、任务类型管理员 replay 仍须后续批次实现，不能将 fixture 视为生产模型。
