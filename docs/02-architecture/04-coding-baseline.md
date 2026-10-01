# 02.4 编码入口与跨模块约定

状态：设计候选 1.1.0。见 [审计结论](../08-delivery/03-design-audit.md)；本文件不替代主题规范。

## 1. 首期边界

一次巡检项提交 1–3 张静态图片；第一张总览，其余为显式关联的细节图。支持质量、瓶/标签检测、OCR、实体候选、可见关系、规则候选、人工复核和整改闭环。不支持视频、在线 LLM、自动风险定性或跨主机 GPU 调度。

## 2. 技术栈与代码边界

| 目录 | 实现职责 | 禁止事项 |
|---|---|---|
| apps/web | Vue 3、TypeScript、Vite、Element Plus、Vue Router、TanStack Query；Pinia 只管会话/UI | 复制状态机、长期对象凭据 |
| apps/api | Python 3.11、FastAPI、Pydantic v2；路由调用命令/查询 | 加载模型，外部调用持有 DB 事务 |
| apps/worker | Celery、Redis、持久任务、AI client、规则、裁剪、导出 | 覆盖历史结果 |
| apps/ai_inference | Python3.11独立FastAPI supervisor + spawn计算子进程；D-FINE-N四类ONNX/PP-OCRv4 CPU；建议检测CUDA FP32，CPU功能保留；业务API/Worker同为Python3.11，依赖环境仍隔离 | 业务ORM、数据库、Redis、最终风险 |
| packages/domain | 状态、guards、权限动作、不变事实类型 | 通用 status setter |
| packages/application | 命令处理器、查询、unit_of_work；每命令唯一入口 | 路由另写业务逻辑 |
| packages/persistence | SQLAlchemy 2、MySQL 8.0.16+、Alembic、Outbox repository | 省略租户条件 |
| packages/storage | 业务环境的 S3 SDK 适配；同源本地签名、内网固定版本读取 | 默认凭据链、事务内网络操作、将摘要匹配当成图像 ready |
| packages/rules | evaluate(facts,bundle,reference_date) 纯函数 | 网络、系统时间、动态 eval |
| contracts / tests | 生成协议；contract/unit/integration/e2e/fault/evaluation 分层 | 模拟成绩冒充真实评测 |

依赖：HTTP/任务入口 → application → domain；persistence 实现仓储接口。AI 只共享协议和图像变换包。应用依赖锁文件和镜像 digest 在实施环境验证后提交；没有完成依赖兼容性验证前不得宣称环境可复现。

统一运行时依据见[ADR-PY-01](07-python-runtime.md)；I-01A开发入口与测试见[开发指南](../../DEVELOPMENT.md)。统一minor不合并AI与业务依赖，也不改变进程/权限边界。

## 3. 事务模板

1. 会话给出租户，服务端授权；不接受客户端 tenant_id 作为权限依据。
2. 校验 Schema、幂等键；锁幂等行。
3. 按 tenant_id+id SELECT FOR UPDATE 聚合根；校验 expected_version、状态、关联对象、证据与版本。
4. 更新聚合、version+1、updated_at；追加审计、Outbox、任务；保存幂等响应。
5. 原子提交后返回。对象/AI/Redis 调用在事务外；失败回滚所有业务写入。

统一锁序：幂等 → inspection → item → run → finding → remediation_task → task_run；同类多行按 ID 排序。数据库死锁最多重跑事务 3 次，重新检查守卫；不能重复执行外部副作用。

I-02B 身份事务补充：短事务认证预检结束后再调用 Redis，提交事务内必须重新认证、授权和校验 CSRF，预检结果不作为提交凭据。用户列表在显式 REPEATABLE READ 事务中完成 COUNT 与分页。禁用用户先定位 session 的租户范围（不是授权），取得 tenant 排他锁，再 claim 幂等记录、重验会话并锁目标 user；这是认证排他锁前置，不改变上述业务聚合锁顺序。原因是幂等 INSERT 的租户/用户外键会隐式取得共享锁，之后升级 tenant 排他锁会引起并发禁用死锁。最后管理员保护采用当前锁定读，不能依赖等待前的旧快照。实现与故障证据见 [I-02B 验收](../08-delivery/11-i02b-identity-api.md)。

I-02C 的 grantRole/revokeRole 复用上述身份排他锁顺序：tenant → 幂等 → 重新认证 → 目标 user/role assignment → 撤会话与审计/幂等响应。User.version 与 session_epoch 在同一事务递增，不能仅修改 user_roles 而保留旧会话。角色列表同样使用一致性快照与租户范围校验；授权后的权限投影容量在提交前检查。见 [I-02C 验收](../08-delivery/12-i02c-role-api.md)。

I-02D 组织/模板/巡检草稿沿用事务模板，新增事务内白名单资源投影与父对象共享锁；clone 按 family 根→源模板锁定，再当前读分配下一 revision，同 family 不同源版本也必须串行。重放先重验会话/动作/结果可见性，再返回原响应，不重复版本/状态前置。会话认证保持 user/roles/epoch 共享当前读，仅 session 行 FOR UPDATE；禁止以 sessions JOIN users FOR UPDATE 将已有用户共享锁隐式升级，避免同用户多会话死锁。见 [I-02D 验收](../08-delivery/13-i02d-foundation-api.md)。

I-02E 巡检项查询：预检结束后进行事务外读限流，再于 RR 事务内重新认证；先授权父巡检/资源及可选实验室，再 COUNT/分页。业务投影使用同一快照，不混用业务锁定读。GET 不改业务状态/版本/审计/任务（既有会话 idle touch 除外）。allowed_actions 只复用原命令守卫，当前写命令未实现所以能力集为空；后续必须同批接通真实命令和完整可信上下文加载。见 [I-02E 验收](../08-delivery/14-i02e-item-query-api.md)。

I-02F1 上传授权补充：预检结束后执行普通写限流与独立上传限流，提交事务锁序为 tenant 共享锁 → 当前 user 排他锁（用户配额 mutex）→ 幂等 claim → 重新认证/CSRF → owner → uploads 配额/插入 → 审计/幂等响应。user 排他锁必须早于认证或幂等外键的 user 共享锁，避免同用户多会话 S→X 升级死锁；其他命令不得照搬排他认证。Item owner 按 inspection→item 锁定，Task owner 锁 remediation_task。配额用当前锁定读，避免等待 mutex 后沿用旧 RR 快照。SDK presigning 使用显式静态凭据，仅本地密码学、无网络/凭据刷新，因此可随响应在事务内保存；HEAD/GET/PUT 等对象网络操作仍必须在事务外。重放重验当前权限/owner，返回原 URL/expiry/request_id，不能重签或续期。见 [I-02F1 验收](../08-delivery/15-i02f1-upload-grant-api.md)。

I-02F2 上传完成：准备阶段按与 F1 相同的 tenant 共享锁→当前 user 排他锁→幂等→重新认证/CSRF 顺序，定位 upload 后先锁 owner，再锁 upload/image。首次未完成时抛内部准备信号使整个事务回滚（包括 pending 幂等行），退出所有数据库锁后执行 HEAD；提交阶段重新执行相同授权/锁序并检查 grant 未过期、准备快照未变化、HEAD 匹配。只在最终短事务原子写入 image/任务/TaskDispatch/upload/审计/幂等，不在网络等待期间持有事务。并发胜者已登记图像时复用胜者记录，不能用迟到 HEAD 覆盖固定版本。getImage 使用 RR 快照、当前 owner.read 和白名单元数据，不做对象 I/O。详见 [I-02F2 验收](../08-delivery/16-i02f2-upload-completion-api.md)。

## 4. 类型与不变量

- 资源 ID 默认 UUID v4 小写；确定性检测/裁剪 ID 按 AI 文档使用 UUIDv5。时间为 UTC RFC3339 毫秒，DB DATETIME(3)。reference_date 在提交时按 tenant.timezone 生成并冻结。
- 通用字符串 NFC 去首尾空白；密码和 OCR raw_text 不处理；账号 NFKC+casefold 后唯一。
- JSON哈希：UTF-8、键字典序、无空格、禁止NaN、保留数组顺序；共享规范函数为json.dumps(sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False)。Python3.11生产者使用同一函数和黄金样本测试；不是任意语言默认JSON字符串。原图/制品哈希直接对字节计算。
- trace_id 为 32 位小写十六进制；request_id/attempt_id 是 UUID。
- 原图、分析图、裁剪图各自保留对象 key、version、SHA，禁止混用。
- 终态结果、事实正文、发布规则/词典正文不可覆盖；元数据只能通过规定命令变化；activation 只影响新 run。
- 所有并发命令使用目标聚合 version；409 VERSION_CONFLICT 时刷新，不自动覆盖。

## 5. 必读规范

1. [状态机](02-domain-model.md)、[AI 进程](03-ai-inference-process.md)。
2. [持久任务](05-durable-jobs.md)、[数据约束](06-data-persistence.md)。
3. [接口语义](../03-interfaces/01-api-contract.md)、[规则算法](../04-ai-rules/01-data-and-rules.md)、[安全](../06-security/01-security-privacy.md)。
4. [前端](../05-frontend/01-interaction-spec.md)、[测试](../07-quality-operations/01-testing-evaluation.md)、[部署](../07-quality-operations/02-deployment-operations.md)。
5. [模型与数据基线](../04-ai-rules/02-model-data-plan.md)、[分层验收政策](../07-quality-operations/04-acceptance-policy.md)、[设计/实施/发布门禁](../08-delivery/05-stage-gates.md)。
6. [确定性事实提取](../04-ai-rules/03-fact-extraction-contract.md)、[IRR关闭清单](../08-delivery/06-implementation-readiness.md)。开工按关闭后的状态/容量/nullable/S3规范，不复制旧审计反例行为。

## 6. 判定原则

结构检查不能替代语义审计。每个命令必须有前置条件、写集、权限、错误、用例；算法适配、故障恢复必须有明确预期。设计可编码不等于人工批准或可上线。
