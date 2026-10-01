# I-02C 验收：角色列表、授予与撤销 API

日期：2026-09-30。承接 I-02A/B，基线为已合并 PR #8 后的 `7fe593f`。当前分支 `wsq/i-02a-auth-api`，本批没有提交、Push、新建 PR 或合并 main。

状态：角色 API 与真实数据库验收通过；整个 I-02 仍在实施。本轮按用户要求同步更新[实施计划](01-implementation-plan.md)的 I-0 进度表，保留原任务验收含义，并区分已合并、本地完成与未完成。

## 1. 本批交付

| 方法与路径（前缀 /api/v1） | operationId | 请求/响应 |
|---|---|---|
| GET /users/{id}/roles | listUserRoles | page、page_size、可选 laboratory_id → 200 RoleAssignmentPage |
| POST /users/{id}/roles | grantRole | RoleGrant → 200 User |
| POST /users/{id}/revoke-role | revokeRole | RoleRevoke → 200 User |

沿用[公共契约](../../contracts/public-api-v1.yaml)，没有新增字段、响应包装、role enum 或数据库迁移。三接口均要求当前有效的 tenant 级 safety_admin；不能使用旧会话权限缓存或客户端 tenant_id 授权。加上 I-02B，身份管理现有 10 个公开接口；不是完整业务 API。

实现位置：

- [HTTP 适配](../../apps/api/app/identity.py)：RoleCommand 严格类型、六角色枚举、实验室 UUID、请求头/Origin、分页查询白名单与三条路由。
- [应用服务](../../packages/application/identity.py)：复用限流、事务内再认证、租户排他锁、幂等与目标用户可见性检查。
- [角色仓储](../../packages/persistence/roles.py)：角色作用域、用户版本、assignment 写入/精确删除、epoch/会话撤销及审计。
- [权限投影](../../packages/domain/security.py)：getMe/login 与授权容量守卫共用去重后的 action/laboratory 投影，避免新增授权导致用户无法登录。
- [数据库测试](../../tests/persistence/test_roles_mysql.py)、[HTTP 边界测试](../../tests/business/test_role_boundary.py)：本批回归入口；身份/Redis 夹具集中到 tests/persistence/conftest.py，仍只使用可丢弃测试实例。

## 2. 精确行为

### 2.1 请求和资源版本

RoleGrant 与 RoleRevoke 均要求四个字段：expected_version、role、scope_kind、laboratory_id。laboratory_id 即使为 null 也必须显式传入；expected_version 必须为 1–2147483647 的整数，不能是 bool 或字符串。路径 user_id、实验室 UUID 由适配层规范化为标准字符串。

expected_version 指向目标 **User.version**，不是 RoleAssignment.version。客户端先查询目标用户的版本；成功授予/撤销各递增 User.version 和 session_epoch 一次。不得自动把最新版本填入冲突请求。新 RoleAssignment 自身 version=1，撤销精确删除该 assignment，不将用户其他作用域授权一起删除。

两种请求都没有 reason 字段；本批不擅自改变既有契约。审计由服务端写入固定操作原因 Grant role / Revoke role，changes 包含 user_id、role、scope_kind、laboratory_id，resource_id 是对应 assignment 的 ID。未来若要求操作者填写原因，应单独评审契约变更。

### 2.2 作用域和状态

| 角色 | 合法 scope_kind | laboratory_id |
|---|---|---|
| safety_admin、rule_expert | tenant | 必须 null |
| lab_manager、inspector、remediator、viewer | laboratory | 必须同租户实验室 UUID |

- 角色/scope 组合不合法为 422；不存在或跨租户的目标用户/实验室为 404。不存在的角色配置不是自动补建，返回 503。
- 授予仅允许 active 用户、active 实验室；disabled 用户或 archived 实验室新增授权为 409。撤销仍可清理禁用用户或归档实验室的已有角色，但不恢复用户状态。
- 新 key 对重复 assignment 发起授权返回 409 STATE_CONFLICT；撤销不存在的精确 assignment 返回 404。先检查 User.version，旧版本仍为 409 VERSION_CONFLICT。失败不递增版本、不撤销会话、不提交幂等或审计。
- 同租户有效 admin 能查看禁用用户/归档实验室的实际授权，以便清理。可选 laboratory_id 仅筛选该实验室 assignment，不混入 tenant 级角色；跨租户筛选在 COUNT 前拒绝为 404。
- 列表默认 page=1、page_size=20，最大 100，排序 created_at DESC,id DESC；总数和页数据在同一 REPEATABLE READ 快照，未知/重复 query 为 422，不承诺跨页冻结。
- 授予后的去重 Session.permissions 最多 200 个 action/laboratory 对；超出返回 409 并原子回滚，不能截断权限清单，也不能仅限制 assignment 数量代替投影容量。

### 2.3 锁、幂等与撤权

1. 沿用 I-02B 的有效会话/CSRF/admin 短预检与事务外 Redis 限流。
2. 提交事务先定位 tenant/actor，取得 tenant 排他锁，再 claim 幂等记录；避免幂等外键共享锁升级排他锁的死锁。随后重新认证/授权/CSRF，并锁定目标用户。所有操作在同一个应用事务中提交或回滚。
3. completed 幂等记录也要重验有效会话、admin 与目标用户可见性；只有合法重放才返回原 HTTP 状态、request_id 和规范 JSON 响应。不同正文仍为 IDEMPOTENCY_CONFLICT。
4. 新命令检查目标用户版本、合法作用域、assignment 状态和最后管理员约束，再写角色、version/epoch、撤销该用户全部会话、追加审计、保存幂等响应。
5. 最后一名 active safety_admin 不能被撤销；tenant 排他锁与当前锁定读用于防止两名管理员同时自撤权、互相撤权导致零管理员。等待锁后 actor 权限也须重新加载。
6. 沿用完整事务死锁最多 3 次重试与锁超时 409/Retry-After:2，不重发 Redis 外部操作，不篡改客户端版本。没有契约对应的角色 Outbox 事件，本批不臆造消息。

修改自身角色时，当前 session 也会被撤销。首次成功可以返回更新后的 User，但旧 cookie 的任何后续访问或缓存重放都为 401；必须重新登录。重新登录后仍具 admin 权限的同 actor 可重放原 key，不再修改角色；已丧失 admin 的 actor 不得取得缓存成功响应。

tenant 排他锁刻意串行化该租户的身份管理写入；这是正确性保护，不是高并发吞吐证明。远端数据库、生产吞吐、可信反代、密钥轮换与完整服务探针均未在本批放行。

## 3. 接入方式

沿用[开发指南 3.6](../../DEVELOPMENT.md)的 API_IDENTITY_ENABLED=1、dev/test、专用回环 MySQL/Redis、HTTPS PUBLIC_ORIGIN 和 CSRF_KEY_FILE，无新增环境变量/依赖。没有独立角色开关；仍遵守 Secure Cookie、同源 JSON、单一 X-CSRF-Token 和 Idempotency-Key。

调用顺序：管理员登录 → GET /users/{id} 读取目标版本 → POST /users/{id}/roles 或 /revoke-role → 使用返回的 User.version 作为后续新意图的版本。重试同一网络请求保留 key 和正文；同一角色在另一实验室是独立 assignment。跨实验室测试用的组织数据来自显式 synthetic fixture，本批没有偷偷实现组织创建 API。

前端可用 GET /users/{id}/roles 展示实际 scope；它不是 Session.permissions 的替代或客户端授权凭据。当前仓库尚未实现角色管理 UI。

## 4. 验证与证据

### 4.1 执行命令

~~~powershell
python -m pytest tests/business tests/domain tests/security tests/persistence/test_unit.py -q --tb=short
$env:LABSAFE_TEST_REDIS_SERVER = '<本机 redis-server 可执行文件>'
python tools/database/run_mysql_tests.py --mysqld '<本机 mysqld 可执行文件>'
python -m ruff check apps packages tests tools/database
python -m ruff format --check apps packages tests tools/database
~~~

MySQL 启动器创建临时目录、随机回环端口与凭据，并核对可丢弃 schema；Redis fixture 核对自己启动的 PID。结束只关闭自己创建的进程，不操作已有服务。CI 继续复用原 persistence job 和独立 Redis 容器；未取得本分支远程 CI 结果。

### 4.2 本地结果

| 检查 | 实际结果 |
|---|---|
| 累计单元/HTTP边界/领域/安全/持久化单元 | 612 passed；相对 I-02B 新增 15 个用例 |
| 隔离持久化套件 | 93 passed，无 skipped；含原 67 项和新增 26 项角色集成用例 |
| 真实数据库证据 | MySQL 8.0.33、0001_initial、43 表、115 外键，结构 errors=[]；93 项中 21 项为与上一行重叠的单元用例、72 项为真实 MySQL 用例 |
| 静态检查 | Ruff check 全部通过；format --check 为 72 个 Python 文件通过 |
| 业务进程/协议 | 6 个业务进程/启动 smoke 已包含在上述 612 项内；业务环境另跑 4 个协议用例通过 |
| AI 独立环境 | 15 个 AI fixture + 4 个协议用例通过，含独立 HTTP 进程及不导入业务/模型依赖检查；不声称真实模型运行 |
| 设计/生成契约 | 16 份生成物无漂移；148 项 synthetic 就绪检查；完整设计校验 PASS，2 份 OpenAPI、193 个文档链接 |

覆盖六种角色、八种非法 role/scope 组合、跨租户/实验室、无 admin 权限、分页排序/筛选、精确删除、禁用/归档清理、登录权限更新、全部旧会话撤销、同 key 重放/正文冲突、新 key 重复授权、过期版本、同 key 十并发与同版本双并发、双 admin 自撤/互撤、审计故障回滚，以及 200 个权限投影容量边界。

最终集中检查日期为 2026-09-30，Python 3.11.4；完整设计校验时间 08:23 UTC（16:23 Asia/Shanghai）。所有记录为本轮实际执行且退出码 0 的结果；两个套件均含 21 个持久化单元用例，不能将 612 与 93 直接相加作为唯一用例数。设计检查的 synthetic 数据不是实际 GPU、对象存储、反向代理或真人审批证据。

本批曾因工具自动审批服务返回 503，导致两次数据库命令与一次文档 patch **未执行**；权限服务恢复后重新执行。最终 93 项隔离持久化套件完整通过，未绕过审批或改为连接用户现有数据库。第三方 Starlette/AnyIO 弃用警告与 I-02B 相同，没有擅自变更依赖版本。

## 5. 未完成范围与下一步

整个 I-02 仍未完成。下一批优先组织/实验室/位置与巡检基础 API，随后实现上传 grant/complete、对象版本/SHA 验证及事实/评估/整改完整事务适配。I-02A 守卫必须接入服务端加载的完整可信聚合和同源 allowed_actions，不能以本批角色 CRUD 替代业务闭环。

Outbox/inbox、持久任务与 Worker→AI 闭环仍属 I-03；D-FINE-N/GPU 真实运行和识别验收仍属 I-ML 主线。角色安全 API 本地通过不等于生产批准、真人独立安全评审或前端联调完成。

## 6. 回退与协作

没有新迁移。紧急开发回退可关闭 API_IDENTITY_ENABLED 并重启，停止整个身份 API；这不会自动恢复撤销的角色或会话，也不能代替数据补偿。角色恢复必须由仍有效的 admin 以当前版本执行明确的新授权意图，产生新审计；不能恢复旧会话 token、删除审计或手动回拨 epoch。

本轮已只读 fetch origin，HEAD 与 origin/main 无提交差异；本地 I-02A/B/C 改动仍未提交。实施计划原有排版此次按用户授权统一表格并新增进度列，不再宣称该文件保持任务前哈希。后续提交/Push/PR 需用户授权，遵守[协作规范](../../CONTRIBUTING.md)，安全变更须独立协作者审查。
