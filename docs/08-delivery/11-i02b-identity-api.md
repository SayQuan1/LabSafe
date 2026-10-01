# I-02B 验收：认证、用户 API 与事务链路

日期：2026-09-30。基线：I-01B PR #7、I-01C PR #8 已合并，当前特性分支 `wsq/i-02a-auth-api`；承接 I-02A 领域守卫。

状态：I-02B 本批实现已通过本地回归。本文不把整个 I-02、生产部署或真实模型验收标记为完成。当前改动尚未提交、Push 或创建新 PR；旧 PR 的 CI 不代表本批 CI。

## 1. 交付范围

### 1.1 已实现接口

字段、枚举和响应投影以 [公共契约](../../contracts/public-api-v1.yaml) 为准。全部路径均以 `/api/v1` 开头。

| 方法与路径 | operationId | 权限与结果 |
|---|---|---|
| POST /auth/login | login | 同源 JSON；有效账号登录，200 Session + 新 session cookie |
| GET /me | getMe | 有效会话；200 Session，含 CSRF、权限和服务端环境 |
| POST /auth/logout | logout | 有效会话、CSRF、幂等键；撤销当前会话并清 cookie，200 Ack |
| GET /users | listUsers | 同租户 admin；一致性快照 COUNT + 分页，200 UserPage |
| GET /users/{id} | getUser | 同租户 admin；200 User，不可见或跨租户 ID 为 404 |
| POST /users | createUser | admin；用户名规范化、初始密码验证，201 User；不会自动授予角色 |
| POST /users/{id}/disable | disableUser | admin；expected_version、active 状态、最后管理员保护；200 更新后的 User |

列表默认 `page=1,page_size=20`，最多 100 条，按 `created_at DESC,id DESC` 排序。未知/重复查询参数（包括用户列表不适用的 laboratory_id）返回 422。User 投影不包含 password_hash、session_epoch、租户内部字段或会话凭据。

### 1.2 实现入口

| 代码 | 唯一职责 |
|---|---|
| [HTTP 适配](../../apps/api/app/identity.py) | 输入 schema、Origin/写头校验、cookie、公开错误和稳定 JSON 响应 |
| [应用服务](../../packages/application/identity.py) | 预检、事务内重验、事务/锁顺序、审计与幂等响应原子提交 |
| [限流器](../../packages/application/rate_limit.py) | Redis 原子计数、登录成功释放、读降级限流和就绪检查 |
| [用户仓储](../../packages/persistence/users.py) | 租户限定查询、账号规范化、创建/禁用、User 白名单投影 |
| [安全原语](../../packages/persistence/security.py) | 会话、RBAC、HMAC 请求摘要、幂等租约、最后管理员保护 |
| [应用入口](../../apps/api/app/main.py) | 显式启用、身份就绪范围、关闭自建数据库/Redis 资源 |

HTTP 路由不自行写 SQL，也不构造来自客户端 tenant_id 的可信 Principal。没有新增或改写数据库迁移、模型协议、状态机枚举或依赖清单。

## 2. 安全与事务约束

### 2.1 请求、密码与会话

- 写请求必须为 application/json 且单一 Origin 与 PUBLIC_ORIGIN 完全匹配；除 login 外要求单个 Idempotency-Key 与 X-CSRF-Token。login 同样防登录 CSRF，不接受跨站登录。
- Pydantic 使用 strict + extra forbid；版本 bool 不能冒充整数。错误不回显密码、请求正文、SQL、数据库 URL 或内部异常文本。全部响应设置 no-store。
- 本批身份 JSON 设 64 KiB 防护上限，超限为 422 VALIDATION_ERROR；它不是图片大小限制。当前中间件作用于已实现的 /api/v1 路径；后续新增普通 API/import 前应按安全规范拆分 2 MiB/10 MiB 的路由限额。
- 用户名 NFKC + casefold 后唯一，display_name 使用 NFC/trim；密码保持原文、不截断，12–128 字符，Argon2id 参数沿用 I-01C。契约初始密码字段的传输上限为 256，129–256 在业务安全策略层返回 422，而不是放宽密码策略。
- Cookie 为 Secure/HttpOnly/SameSite=Lax/Path=/，绝对有效期 8 小时、空闲期限 30 分钟。没有不安全 HTTP cookie 开关。禁用用户增加 version/session_epoch 并撤销全部会话。
- 不处理未经信任的 X-Forwarded-For，Uvicorn `proxy_headers=False`；限流使用直接连接 IP。受控反代部署的可信代理 allowlist 尚未落地，因此此阶段反代后不能声称按最终终端 IP 限流。

### 2.2 事务顺序

1. HTTP 完成结构边界校验。短认证预检校验会话、必要的 admin 权限和 CSRF，然后结束事务。
2. Redis 限流在数据库事务外执行。预检结果只用于计数主体，不能跳过提交事务内的认证或授权。
3. 提交事务显式使用 REPEATABLE READ。通过 session 定位 tenant/actor（仅定位，不授权），claim 幂等记录，然后重新认证、校验 CSRF/权限与目标资源可见性。
4. 用户禁用例外：先取得认证 tenant 排他锁，再 claim 幂等记录，随后重新认证并锁目标 user。否则幂等 INSERT 的外键隐式共享锁升级为 tenant 排他锁，会引起并发死锁。业务聚合仍在幂等之后锁定。
5. 校验 expected_version、active 状态和最后管理员保护；当前锁定读读取角色和活跃管理员，禁止使用等待锁之前的旧快照。写业务状态、审计、完整幂等响应后原子提交。任何失败回滚整个命令。
6. MySQL 1213 最多执行 3 次完整事务，每次重新认证/校验，不变更 expected_version，也不重复 Redis 外部操作；锁等待 1205/NOWAIT 3572 返回 409 REQUEST_IN_PROGRESS + Retry-After:2。

本批没有对应契约事件的用户/会话 Outbox 消息，不臆造 UserCreated 等事件。巡检事务的业务写入 + 审计 + Outbox/任务 + 幂等响应仍需后续实现，不能以本批身份事务替代整个 Unit of Work。

### 2.3 幂等与撤权

- 作用域 tenant、actor、POST、规范化路径、key_hash；key 为 8–128 个可见 ASCII 字符，按 opaque 值处理，不 trim/NFC。记录保留 24 小时，pending 租约 30 秒。
- 身份请求正文采用规范 JSON + HMAC-SHA256 摘要，使用服务端秘密防止无密钥离线猜测 initial_password；不在幂等表或审计表存密码。
- 同 key 同正文重放原状态、原 request_id 和统一键序 JSON 字节；同 key 不同正文为 409 IDEMPOTENCY_CONFLICT。活 pending 不允许第二请求冒领 owner；过期 owner 无法 complete，新 owner 接管受到 fencing 校验。
- 缓存重放前仍重新认证/授权与检查资源可见性；禁用或撤权后不能读取缓存成功响应。logout 首次成功撤销当前会话，旧会话重放应为 401，不承诺再次返回缓存 200。
- CSRF 密钥同时用于身份幂等 HMAC；开发期间必须保持稳定。更换该密钥会使旧会话校验失败，并可能让旧幂等键返回正文冲突；本批不提供无损密钥轮换。

### 2.4 限流和依赖故障

| 场景 | 实际行为 |
|---|---|
| 登录 | 按 tenant + 规范账号 + 直接 IP，固定 15 分钟窗口，最多 5 个失败/在途占位；成功释放本次占位；第 6 次为 429 |
| 已认证用户 | 普通 120/min，写请求另外 60/min；Redis Lua 原子计数与 TTL |
| Redis 计数失败 | login/write 为 503，不进入业务写事务；GET 降级为进程内最多 30/min、最多 10000 个主体的保守计数，并限频告警 |
| 登录提交后 Redis 释放失败 | 503 且不下发 cookie；MySQL 已有一个未交付、会过期的 session 和登录审计，不能声称 Redis/MySQL 原子提交 |
| 数据库连接失败/迁移版本不匹配 | 身份 /ready 为 503；SQL/连接串不透传，/health 仍只表示进程存活 |
| 审计或幂等写入失败 | 同事务业务写入回滚，不返回伪成功 |

错误码按 [错误目录](../../contracts/error-codes.json) 暴露：底层 AUTHENTICATION_REQUIRED/INVALID_CREDENTIALS 映射 UNAUTHENTICATED，CSRF_FAILED 映射 FORBIDDEN，LAST_ADMIN/IDEMPOTENCY_EXPIRED 映射 STATE_CONFLICT。

## 3. 接入与运行

准确命令见 [开发指南 3.5–3.6](../../DEVELOPMENT.md)。默认 API_IDENTITY_ENABLED=0；设为 1 时要求：

1. Python 3.11 业务环境，APP_ENV=dev/test；不能以此批代码开启 production。
2. 专用回环 MySQL 8.x，迁移版本 0001_initial，CLI verify 通过；运行账户与迁移账户分离，连接串通过 DATABASE_URL_FILE 读取。
3. 已准备的专用开发 Redis，显式 REDIS_URL；运行模式不自动创建临时数据库或 Redis，只有测试启动器创建隔离实例。
4. CSRF_KEY_FILE 指向仅 API 可读的至少 32 个原始随机 bytes；首次排他创建，不在重启时覆盖，不复用 AI 令牌。
5. PUBLIC_ORIGIN 为准确 HTTPS origin；本地反代在同 origin 提供前端与 API，并使用可信开发证书。仓库尚未提供登录 UI/TLS 代理联调交付。

/ready 在未启用时只报告 process-only 范围；开启后只核对迁移版本和 Redis ping，不代替 schema verify、不检查对象存储/Worker/巡检业务/真实模型。返回中仍有开发环境与 is_simulated 标识，不能拿它作为生产健康门禁。

## 4. 测试证据

### 4.1 可复现入口

| 层次 | 命令 |
|---|---|
| 单元/HTTP/领域/安全 | `python -m pytest tests/business tests/domain tests/security tests/persistence/test_unit.py -q --tb=short` |
| 真实数据库与身份集成 | 设置 LABSAFE_TEST_REDIS_SERVER 后，`python tools/database/run_mysql_tests.py --mysqld <mysqld可执行文件>` |
| 代码质量 | `python -m ruff check apps packages tests tools/database`；`python -m ruff format --check apps packages tests tools/database` |
| 业务/协议进程回归 | `python -m unittest discover -s tests/business -t . -v`；tests/protocol 同命令 |
| AI 独立环境回归 | AI Python 执行 `python -m unittest discover -s tests/ai -t . -v`；tests/protocol 同命令 |
| 设计/生成契约 | 独立设计依赖环境执行 `python -B tools/design/build_specs.py --check`、`test_readiness_design.py`、`validate_specs.py` |

本地数据库启动器使用临时 data directory、随机回环端口、随机凭据和明确可丢弃库。Redis 测试夹具启动自己的临时进程，核对 PID；结束只关闭自己的进程，不连接/停止/清空操作者已有服务。

### 4.2 覆盖内容与修复记录

- 7 个 HTTP 接口与 User/UserPage/Session/Ack 响应 schema、登录 cookie、非法 Origin、CSRF、未知输入、跨租户、未授权访问、密码不回显。
- 同 key 10 并发只创建一名用户、一条审计和一条幂等记录；成功响应字节一致。同 version 禁用竞争仅一次成功，另一请求 VERSION_CONFLICT。
- 两名 admin 同时禁用自己或互相禁用，最终至少保留一名活跃管理员；等待后的身份和角色重新验证。
- preflight 后撤销 session/权限，提交事务分别拒绝 401/403且不残留已提交幂等记录；已撤销会话不能重放缓存成功结果。
- 审计失败回滚、Redis 故障无新增业务写入/会话、GET 保守降级、就绪失败、账号规范化冲突、密码上限、pending/过期 owner fencing。
- 明确测试 Redis 调用不发生在应用数据库事务内；事务实际隔离级别为 REPEATABLE READ；登录提交后释放故障不交付 cookie，但不会假报 MySQL 回滚。

首轮真实数据库回归发现两项失败：MySQL JSON 重排造成重放字节不同，以及外键共享锁升级造成禁用竞争死锁。已分别修复为统一规范 JSON 响应、tenant 排他锁前置与完整事务死锁重试；不是放宽测试断言来掩盖冲突。

### 4.3 最终结果

2026-09-30 在 Python 3.11.4、独立业务/AI 环境完成最终回归，所有下列命令退出码均为 0：

| 检查 | 本地结果与证据边界 |
|---|---|
| 单元/HTTP/领域/安全/持久化单元 | 597 passed；不连接外部服务 |
| 隔离 MySQL/Redis 持久化套件 | 67 passed，无 skipped；其中 21 个持久化单元用例与上一行重叠、46 个真实 MySQL 用例；其中 17 个为本批身份/HTTP/故障/并发集成用例 |
| 数据库结构和迁移 | MySQL 8.0.33，0001_initial，43 表、115 外键；升级/降级/重升及结构差异检测通过，最终 errors=[] |
| Ruff check / format --check | 全部通过，69 个 Python 文件格式通过 |
| 业务/协议回归 | 6 + 4 passed；6 个业务用例已包含在 597 中，不重复计为新增 |
| AI 独立环境 | 15 个 AI + 4 个协议用例通过；未引入业务/数据库/模型依赖，仍仅为开发 fixture 验收 |
| 设计生成物 | 16 份无漂移；94 个公共 operation、154 个 schema，与既有契约保持一致 |
| 就绪设计/完整设计校验 | 148 项 synthetic 检查通过；完整 PASS，2 份 OpenAPI、174 个文档链接；不能替代真人审批或真实模型性能验收 |

完整设计校验时间 07:44 UTC（15:44 Asia/Shanghai）。设计依赖使用已有独立工具目录；未安装新依赖、未改 requirements、未刷新生成报告来掩盖差异。单元/集成套件均报告一个 Starlette/AnyIO 第三方弃用警告，不是测试失败；后续升级依赖时统一处理，不在本批擅自换版本。

CI 已增加 pytest 业务/领域/安全入口和独立 Redis service，并向持久化启动器传递本 job 的 Redis 随机端口；**仅完成配置与本地验证，尚无本分支远程 CI 结果**。原用户实施计划文件 SHA256 与任务前一致；未纳入提交，暂存区为空。

## 5. 下一批关闭清单

| 顺序 | 尚未交付 | 进入下一批的可执行验收条件 |
|---|---|---|
| 1 | 角色列表/授予/撤销 HTTP API | scope/tenant/laboratory 校验、重复 grant、最后管理员、改权全会话撤销、幂等重放与并发测试；复用当前认证锁序 |
| 2 | 组织/实验室/位置与巡检基础 API | tenant 过滤、父子关联、版本/状态守卫、审计、完整只读投影与受限分页 |
| 3 | 上传 grant/complete 与图片可用性 | 对象版本/SHA、真实 MIME/容量校验、不可变引用、拒绝未完成/跨 owner/跨租户图片；不在数据库事务内做对象外部调用 |
| 4 | 巡检项/事实/评估/整改命令适配 | 将 I-02A 守卫接入锁定的完整聚合写集；current 指针、版本、审计/任务/幂等原子性与查询 allowed_actions 同源 |
| 5 | I-03 持久任务闭环 | Outbox/inbox、租约/回收/fencing、Worker→AI→数据库闭环；不能用 fixture echo 替代 |

真实 D-FINE-N/GPU 性能和识别验收仍按 I-ML-01 模型主线推进；本批未运行模型、MinIO、真实图片或生产环境。前端联调、受控反代终端 IP、生产依赖账户/密钥轮换和资源留存清理仍需相应阶段关闭。

## 6. 协作与回退

本批在现有特性分支保留代码、测试、CI 和文档；没有自动提交/Push/建 PR，也没有修改 main。用户原有 `01-implementation-plan.md` 本地表格排版未改、不纳入本批交付。后续提交与 PR 遵循 [协作规范](../../CONTRIBUTING.md)，安全/权限/数据库变更仍需独立协作者审查。

无新迁移。开发回退先关闭 API_IDENTITY_ENABLED 并重启，只会停止该 API，不删除用户、session、幂等或审计数据；不能把关闭开关视为已撤销数据库中的会话。若未来需要代码 revert，应协调依赖该原语的后续功能；不删除审计记录，不对已有业务库执行测试 downgrade。
