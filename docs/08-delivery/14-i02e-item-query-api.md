# I-02E 巡检项查询与动作投影基础验收

状态（2026-09-30）：本批已通过本地实现与验证，尚未提交、Push 或新建 PR。本批仅交付两个查询接口及同源守卫探测基础，不代表整个 I-02 或完整动作适配已经完成。

## 1. 交付范围

| operationId | 方法与路径 | 返回 data | 查询参数 |
|---|---|---|---|
| listInspectionItems | GET /api/v1/inspections/{inspection_id}/items | InspectionItemPage | page、page_size、laboratory_id |
| getInspectionItem | GET /api/v1/inspection-items/{item_id} | InspectionItem | 无 |

路由、字段、枚举沿用[公共契约](../../contracts/public-api-v1.yaml)，不新增或修改生成契约、数据库表或迁移。列表成功 200、默认 page=1/page_size=20、page≤2147483647、page_size≤100，按 created_at DESC,id DESC 排序。未知或重复 query、非法 UUID/分页为 422；详情不接受任何 query。可选 laboratory_id 不能被忽略，也不作为授权依据。

新增两个 operationId 后，累计注册 28 个公共业务 API：身份/用户 7、角色 3、基础业务 16、巡检项查询 2。上传、提交、重试、完成巡检项以及完成/取消巡检均未接入，本批没有写命令、对象调用或推理任务。

## 2. 实现入口与调用顺序

| 层 | 文件 / 入口 | 职责 |
|---|---|---|
| HTTP | apps/api/app/item_queries.py / mount_item_queries | 严格 query、UUID 规范化、session cookie、request_id，分发查询 |
| 应用 | packages/application/item_queries.py / ItemQueryApplication.read | 身份预检、事务外读限流、权威读事务与有界死锁重试 |
| 仓储 | packages/persistence/inspection_items.py / InspectionItemRepository | tenant/父巡检/实验室授权、COUNT/分页快照、公开字段白名单 |
| 领域 | packages/domain/item_actions.py / allowed_item_actions | 在明确启用且上下文齐备时，调用原 submit/retry/complete 守卫 |

执行顺序：短事务身份预检结束 → Redis 读限流 → 新建 REPEATABLE READ 事务 → 重新认证并加载当前角色 → 查询/授权父巡检或巡检项 → 可选实验室筛选授权 → COUNT 与页数据 → 白名单投影。详情同样在重新认证后的事务中查询，不复用预检的角色快照。

认证继续使用 I-02D 的 tenant/user/roles/epoch 共享当前读及当前 session 行锁；业务父巡检、子项、筛选实验室采用普通一致性读，不混用业务锁定读与旧 RR 快照。成功读取可更新既有会话空闲时间，但不得改巡检、子项、版本、事实、评估、Finding、审计、幂等、Outbox 或任务。

事务外 Redis 不可用沿用受限本地读限流；不把此降级扩展到写接口。预检后会话被撤销，读事务必须返回 401。1213 死锁最多重试三个完整读事务，每次重新认证，不重复收费；1205/3572 沿用 409 REQUEST_IN_PROGRESS、Retry-After:2。数据库异常按共享边界脱敏为 503，未预期内部异常为 500，不回显 SQL、连接串或调用栈。

## 3. 可见性、分页与公开字段

### 3.1 授权和空页的区别

- tenant 仅来自服务器 session；客户端 tenant_id query 为 422。跨 tenant ID、无实验室范围、不存在的父巡检/子项均为 404。
- safety_admin 可读同 tenant；inspector、lab_manager、remediator、viewer 仅可读已授权实验室。只有 rule_expert 或无角色不能因此读取实验室巡检项。
- 列表先查父巡检并授权，再做 COUNT；不能以 item 列表为空隐藏父对象不存在，也不能先返回未授权 total。JOIN 同时约束父子 tenant、inspection_id 和 laboratory_id。
- laboratory_id 指向不存在/不可见实验室为 404；同时可见但与父巡检实验室不一致为 200 空页。筛选不能扩展父巡检范围。
- 已授权的空父巡检和超过最后一页均为 200 空页，后者保留真实 total。当前创建 API 始终原子创建子项，空父仅由标注的合成数据库夹具验证查询语义。
- 九种子项状态以及 completed/cancelled 父巡检仍可按权限读取；状态终结不等于删除历史。

COUNT、页数据及状态使用同一请求内快照；COUNT 后其他事务插入/更新不影响本次结果，下个请求可见新状态。分页不是跨请求冻结快照；100 项容量、同时间 id 降序、页中间位置与末尾空页分别验证。

### 3.2 严格响应投影

仅返回 id、created_at、updated_at、version、inspection_id、laboratory_id、location_id、status、submission_revision、current_run_id、current_fact_revision_id、review_outcome、allowed_actions。日期使用 UTC RFC3339 毫秒。

不得泄漏 tenant_id、template_item_id、current_evaluation_id、inspector_id/creator_id、父巡检状态、reviewed_by/at 或数据库其他列。current run/fact 可为真实 nullable 指针；测试同时验证 null 草稿及合法外键历史非 null 投影。合成历史不是实际模型执行或人工完成命令的验收证据。

## 4. allowed_actions 的精确阶段语义

### 4.1 本批 HTTP 始终返回空数组

当前未注册任何巡检项写路由；仓储固定传入空 capability 集和未加载上下文，因此所有状态都返回 `allowed_actions=[]`。这是服务当前没有可执行写能力，不表示该状态永远禁止业务动作。前端不得从状态自行补按钮，也不得把此列表当离线授权凭据。

本批完成的是纯领域探测入口与查询的安全能力门禁；**没有完成可信完整聚合加载器、上传或命令事务，不能称作“提交/重试/完成已可用”**。未加载 evaluation 时不能把 has_unknown 默认为 false；零 Finding 不意味着 no_issue，不能由初始 AI outcome 推导当前 U。

### 4.2 领域入口约定

`allowed_item_actions(actor, item, context, *, enabled)` 只接受服务器已授权快照；入口先检查 tenant/READ 权限。enabled 只能含下表 operationId，未知值抛配置错误；不从 HTTP query 或客户端 JSON 接收。

| 候选动作 | 必须加载的上下文 | 同源判断 |
|---|---|---|
| submitInspectionItem | current_findings、images、selections | 直接调用 submit_item；验证采集人、图片 owner/location/状态、顺序、overview/detail 及确认风险阻断 |
| retryInspectionItem | current_findings、images、old_run | 直接调用 retry_item；验证当前失败 run、固定制品/设备/reference_date、完整旧图片清单 |
| completeInspectionItem | current_findings、evaluation | 分别探测三个合法 ReviewOutcome 是否至少一个能通过 complete_item；不向调用者虚构人工结论 |

ItemActionContext 为 frozen dataclass。`None` 表示未加载，`()` 只表示服务器已核验的穷尽空集合；二者不能互换。候选动作只有同时具备 capability、全部所需上下文且原守卫通过才输出。输出去重并按 operationId 稳定排序。探测是纯读，不改快照或数据库。

complete 探测用当前 version 和固定非空探测原因判断“是否存在合法结论”；真实命令仍须重新验证用户提交的 version、outcome、reason。U 优先于 C，未复核 Finding 阻断全部完成；保留旧守卫全部语义，不复制另一套状态表。仅把 403/404/409/422 业务守卫拒绝视为动作不可用；其他异常继续抛出，不把系统故障伪装成空按钮列表。

### 4.3 后续启用动作的关闭清单

1. 接通对应公共命令路由及唯一应用事务入口，具备当前权限、CSRF、幂等、版本、完整锁集、审计与任务规则。
2. 在服务器可信快照中加载完整 findings、图像/选图或当前 run/evaluation；未知与空值语义不可合并，不能由请求伪造 owner、scope、U 或状态。
3. complete 之前固定并校验当前 rule_evaluations.result 的持久化结果结构，接通从当前事实/评估计算 U 的加载器；不猜测 JSON 格式，不以缺字段或空结果默认安全。
4. 在同一批变更中启用对应 capability，补齐查询动作与真实命令的同源正反例、并发变化后的二次守卫、失败原子回滚。只改 capability 常量不构成完成。

## 5. 本地验证

### 5.1 执行命令

在开发指南规定的业务 Python 3.11 环境，从仓库根目录执行：

~~~powershell
python -m ruff check apps packages tests tools/database
python -m ruff format --check apps packages tests tools/database
python -m pytest tests/business tests/domain tests/security tests/persistence/test_unit.py -q --tb=short
$env:LABSAFE_TEST_REDIS_SERVER = '<专用测试 redis-server 可执行文件>'
python tools/database/run_mysql_tests.py --mysqld '<mysqld 可执行文件>'
python -m unittest discover -s tests/protocol -t . -v
python -B tools/design/build_specs.py --check
python -B tools/design/test_readiness_design.py
python -B tools/design/validate_specs.py
~~~

设计校验依赖独立工具环境；AI fixture/协议须另外在独立 AI Python 环境运行开发指南的测试。持久化启动器创建随机端口、随机凭据的临时 MySQL，Redis fixture 核对自身子进程 PID；不接管已有服务。不得将直接 pytest 跳过 MySQL 的结果记为数据库通过。

### 5.2 实际结果

| 检查 | 实际结果 |
|---|---|
| 累计单元/HTTP/领域/安全/持久化单元 | 961 passed；本批新增 270 项，其中领域 235、HTTP 边界 28、应用边界 7 |
| 完整隔离持久化套件 | 154 passed、无 skipped，最终完整执行退出码 0；本批新增 27 个真实数据库用例 |
| 数据库范围 | MySQL 8.0.33、0001_initial、43 表、115 外键、结构 errors=[]；154 项中 133 项为真实 MySQL 用例，21 项为重复的持久化单元 |
| 静态检查 | Ruff check PASS；90 个 Python 文件 format --check PASS |
| 进程/协议 | 业务环境协议 4 项；独立 AI 环境 fixture 15 项、协议 4 项通过；业务进程 smoke 已计入 961 项 |
| 设计/文档 | 16 份生成物无漂移；148 项 synthetic readiness checks；完整设计 PASS、2 份 OpenAPI、214 个文档链接 |
| 路由/Git | 实际注册 28 个公共业务 operationId；巡检项写路由为 0；git diff --check 通过，暂存区为空 |

以上为 2026-09-30、Python 3.11.4 的实际本地结果。961 与 154 重叠 21 项，不能直接相加作为唯一测试数；两环境协议测试也有重复。保留第三方 Starlette/AnyIO 已有弃用警告；没有运行本分支远程 CI、真实 GPU/模型、对象存储、浏览器 TLS 联调或生产部署，亦无独立审查批准。

新增测试文件：tests/domain/test_item_actions.py、tests/business/test_item_query_boundary.py、tests/business/test_item_query_application.py、tests/persistence/test_item_queries_mysql.py。领域矩阵比较原守卫，覆盖缺上下文、未启用、U/C/未复核、跨范围及异常不吞；HTTP 验 query/UUID、错误/缓存边界；真实数据库覆盖组织→模板→巡检→查询、各角色/跨 tenant/实验室、COUNT 前授权、空页、九状态、历史指针、并发快照、撤会话/改权及读限流。

初次错误封装测试误把人为构造的 ServiceError 消息当作底层异常；已按既有约定分别验证安全领域消息、SQLAlchemy 异常和内部 RuntimeError 脱敏，没有改弱共享安全处理。首轮持久化结果为 152 passed/1 failed，原因是历史夹具使用了非法人工结论；已改为契约枚举 no_issue，不新增 safe 枚举。

复核还发现可选实验室筛选复用了组织仓储的 FOR SHARE 当前读；本批改为普通一致性 SELECT，并在并发插入/更新快照测试中断言所有业务 SELECT 均无 FOR SHARE/FOR UPDATE，保留认证所需的既有锁。未改动前批组织命令锁。

并行启动第二个临时 MySQL 曾在初始化时退出，未运行任何用例；检查时 C 盘仅余约 315 MiB，最终完整回归改用项目内被 Git 忽略的 D 盘 `.pytest_cache` 下随机临时目录，进程级 TEMP/TMP 不修改系统设置、不清理用户文件。初始化失败不计作测试通过；全部修正后重新完整运行 154 项通过，不以首轮的部分通过抵扣。

## 6. 接入、剩余工作与回退

沿用[开发指南 3.6](../../DEVELOPMENT.md)的 APP_ENV=dev/test、API_IDENTITY_ENABLED=1、专用 MySQL/Redis、HTTPS 同源与 Secure session cookie；无新增配置、依赖或迁移。未配置返回 503。读请求不要求写用 CSRF/幂等头，所有响应 no-store，错误带 request_id。

调用路径：登录 → I-02D 创建或查询巡检 → 用 inspection_id 列出巡检项 → 用 item_id 查询详情。前端完整采集界面尚未完成；/ready 仍只检查已接入部分的迁移版本和 Redis，不证明存储、任务或模型就绪。

下一批优先实现上传 grant/complete、对象版本与 SHA 固定/校验、图像验证任务；再接提交/重试/完成的完整聚合事务和可信上下文加载。Outbox/Worker/独立 AI 闭环属 I-03；D-FINE-N/GPU 实际执行属 I-ML，当前 production 保护不变。

回退可关闭 API_IDENTITY_ENABLED 并重启，停用已接入身份/业务路由；不删巡检或审计，不恢复已撤会话，无 schema 回退。后续提交、Push、PR 按[协作规范](../../CONTRIBUTING.md)并经用户授权，本轮不执行。
