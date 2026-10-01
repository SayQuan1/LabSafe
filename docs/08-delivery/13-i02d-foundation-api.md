# I-02D 验收：组织、位置、模板与巡检草稿 API

日期：2026-09-30。承接 I-02A/B/C，基线为 `7fe593f`，仍在 `wsq/i-02a-auth-api`。本批没有提交、Push、新建 PR 或合并 main。

状态：本批实现及本地回归完成，最终成绩见第 5 节；未提交或取得本批远程 CI/独立评审。整个 I-02 尚未完成，不将巡检草稿当作上传、推理、复核和整改闭环。

## 1. 交付边界

### 1.1 本批 16 个接口

所有路径带 `/api/v1` 前缀；沿用[公共契约](../../contracts/public-api-v1.yaml)，没有修改生成的 Schema、错误码目录或数据库迁移。

| 方法与路径 | operationId | 成功响应 | 权限 |
|---|---|---|---|
| GET /colleges | listColleges | 200 CollegePage | read，公共投影 |
| GET /colleges/{id} | getCollege | 200 College | read，公共投影 |
| POST /colleges | createCollege | 201 College | admin |
| GET /laboratories | listLaboratories | 200 LaboratoryPage | read，按实验室范围 |
| GET /laboratories/{id} | getLaboratory | 200 Laboratory | read，按实验室范围 |
| POST /laboratories | createLaboratory | 201 Laboratory | admin |
| GET /laboratories/{id}/locations | listLocations | 200 LocationPage | read，按路径实验室 |
| POST /laboratories/{id}/locations | createLocation | 201 Location | admin |
| GET /templates | listTemplates | 200 TemplatePage | read，普通角色仅 published |
| GET /templates/{id} | getTemplate | 200 Template | read，普通角色仅 published |
| POST /templates | createTemplate | 201 Template | admin |
| POST /templates/{id}/publish | publishTemplate | 200 Template | admin |
| POST /templates/{id}/clone | cloneTemplate | 201 Template | admin |
| GET /inspections | listInspections | 200 InspectionPage | read，按实验室范围 |
| GET /inspections/{id} | getInspection | 200 Inspection | read，按实验室范围 |
| POST /inspections | createInspection | 201 Inspection | capture，按实验室范围 |

结合 I-02B/C 的 10 个身份/角色接口，目前实现 26 个公共业务接口；不包含进程探针，也不代表完整公共契约已实现。

### 1.2 代码职责

| 层次 | 实现文件 | 职责 |
|---|---|---|
| HTTP | [foundations.py](../../apps/api/app/foundations.py) | 严格 DTO、UUID、query 白名单、路由及响应 |
| 应用 | [foundations.py](../../packages/application/foundations.py) | 认证/限流、明确命令分发、事务、幂等和重放授权 |
| 领域 | [foundations.py](../../packages/domain/foundations.py) | 位置父类型、active、模板唯一性、巡检容量守卫 |
| 持久化 | [organizations.py](../../packages/persistence/organizations.py) | 组织与位置的白名单读投影和创建 |
| 持久化 | [templates.py](../../packages/persistence/templates.py) | 模板及子项、发布、family 版本分配与克隆 |
| 持久化 | [inspections.py](../../packages/persistence/inspections.py) | 巡检与全部草稿子项的原子创建 |
| 公共 SQL 辅助 | [foundations.py](../../packages/persistence/foundations.py) | 固定 SQL 的范围过滤、分页、文本和日期投影 |

HTTP 不执行 SQL；表名/列名只来自服务端固定集合，客户端不能指定任意表或 tenant。身份事务、Redis 限流、幂等和审计复用前几批，不另建一套会话协议。

## 2. 输入、权限和可见性

### 2.1 严格输入

- 所有 DTO 禁止未知字段和隐式类型转换；`required` 必须 bool，`sort_order` 是 0–2147483647 的整数，不能用 bool 或字符串代替。
- UUID 正文采用规范带连字符形式并转小写；`LocationCreate.parent_id` 即使为 null 也必须显式传入。模板子项 ID 按既有契约由创建请求传入；模板、组织、位置、巡检及巡检项 ID 由服务器生成。
- `TemplateCreate.items` 为 1–100 项，id/code/sort_order 在模板内分别唯一；文本 NFC 规范化并去除首尾空白后再次检查 code 唯一性。空文本、控制字符或超长输入为 422。
- 同租户重复学院/实验室 code，或与已有模板子项主键碰撞，为 409 STATE_CONFLICT；不能改写已有资源。失败回滚业务行、子项、审计和幂等 claim。
- publish/clone 使用 `VersionCommand`：expected_version 指向**所选源模板的 version**，不是 family revision；reason 保存在审计中。

### 2.2 授权查询

任一有效角色可读取同租户 active 学院的 College 白名单字段，将其作为组织选择器公共投影；没有发布动作的学院以 active 表示可公开，archived 仅 admin 可读。无角色用户不能读取该投影。该规则不外推到用户信息、实验室业务数据或其他租户配置。

普通角色只能读取 published 模板；admin 可读取 draft/published/retired。rule_expert 的 tenant 角色只获得这些公共投影，不因此拥有全实验室读取权限。实验室、位置和巡检始终先按当前授权范围过滤，再做 COUNT 和分页；未知/跨租户/范围外资源为 404，没有任何实验室授权的无过滤列表为 403。

学院、模板列表拒绝 laboratory_id；实验室、巡检列表可按 laboratory_id 筛选；位置列表使用路径中的实验室，不接受同名 query 覆盖。未知/重复 query 为 422。默认 page=1、page_size=20，最大 100；created_at DESC,id DESC，COUNT 与页数据同一 REPEATABLE READ 快照，但不同页请求不冻结成同一视图。

## 3. 精确命令行为

### 3.1 组织与位置

创建实验室要求同租户 active 学院；创建位置要求 active 实验室及同实验室 active 父位置。父类型只有以下组合合法：room 无 parent；area 的 parent 为 room；shelf/cabinet 的 parent 为 room 或 area。

本批没有移动、改父节点或删除位置接口。新 UUID 仅能引用已有且类型合法的父节点，因此不能形成环；后续如果增加移动接口，必须单独实现完整祖先链校验，不能沿用“新建不会成环”的证明。

### 3.2 模板

1. create 建立新 family：family_id=新模板 id、revision=1、version=1、status=draft，并一次写入全部子项。
2. publish 锁定源模板，校验 expected_version、draft 状态和子项，再改为 published、version+1，保存用户 reason。正文不变。
3. clone 先根据源模板取得 family_id，锁 family 根，再锁源模板并校验 expected_version；当前锁定读 family revision 后分配 max+1。
4. clone 创建新 draft、version=1，复制正文并生成全部新的子项 ID；源模板的正文、status、version 均不变化。同 family 从不同源 revision 并发克隆也共享根锁。revision 超出公共整数上限则 409。

已发布模板不改正文；本批无模板编辑/删除/retire API。需要新的采集模板时创建新 draft，或按现有 clone 契约建立同正文新 revision；不能假定已有未实现的编辑端点。

### 3.3 巡检草稿

服务器从当前 session 确定 inspector_id，从模板和位置记录加载可信状态；客户端不能设置巡检状态、创建人或任意子项。

创建前验证 capture 权限、active 实验室、published 模板、1–100 个互异位置及全部位置同实验室且 active；模板项数×位置数必须 ≤100。非 admin 看不到未发布模板时返回 404，admin 使用 draft 模板创建则为 409；重复位置或超容量为 422。

同一个事务创建一个 draft inspection 与 template_items×location_ids 的全部 draft inspection_items，submission_revision=0，current run/fact/evaluation 和人工结论均为空。包括 required=false 的模板项，不自动省略可选项。Inspection.item_ids 与真实子项一致，巡检固定引用本次模板 ID，不追随 family 的后续 clone。

本批只返回 Inspection 契约。尚未实现 InspectionItem 查询，因此不伪造 allowed_actions；完成和取消巡检也未接入 HTTP。

## 4. 事务、幂等与故障修复

### 4.1 提交边界

短事务认证/CSRF 预检 → 事务外 Redis 限流 → 提交事务定位 tenant/actor 并 claim 幂等 → 重新认证、CSRF 与当前权限 → 锁定业务对象/校验 → 业务及全部子项、审计、幂等响应一并提交。数据库失败整事务回滚；Redis 写限流不可用则 503，不开始业务写入。

本批普通基础命令仅持 tenant 共享认证锁，模板 clone 以 family 根为业务互斥锁，不将所有业务写入升级成 tenant 排他锁。改权/禁用仍沿用前批 tenant 排他锁和当前权限重载。基础创建没有对应已定义的 Outbox 事件，不臆造任务；I-03 持久任务仍未完成。

同 key/正文重放保留原状态、request_id 和规范 JSON 字节，不重复资源或审计。重放前验证当前会话、动作权限和结果资源可见性；重放授权采用当前锁定读，避免等待竞争请求提交后仍从旧 RR 快照读取“资源不存在”。

重放不再次执行版本/状态前置条件；例如原巡检创建成功后模板被退休，只要该 actor 仍具有对应实验室 capture/read 权限，旧 key 返回原成功响应，新 key 创建仍拒绝。撤权后旧 session 为 401，重新登录后无 capture/范围则不得取缓存成功响应。

死锁最多重跑完整事务 3 次；锁超时/正在执行返回 409 REQUEST_IN_PROGRESS、Retry-After:2，保留原 key 重试，不能自动刷新 expected_version。

### 4.2 本轮发现并修复的会话锁竞争

首次四会话并发克隆测试出现部分 REQUEST_IN_PROGRESS。原因是 authenticate 先 FOR SHARE 读用户权限，随后 sessions JOIN users FOR UPDATE 又把同一用户行升级为排他锁，多会话持 S 后竞争升级造成死锁。

修复为用户/角色及 epoch 保持共享当前读，只有本次 session 行 FOR UPDATE；tenant→user/roles→session 的顺序和改权排他保护不变。新增同步屏障测试强制四个事务同时持用户共享锁，然后继续认证，不能靠偶然串行通过；并回归全部身份、撤权、最后管理员竞争用例。

## 5. 验证与证据

### 5.1 执行入口

~~~powershell
python -m ruff check apps packages tests tools/database
python -m ruff format --check apps packages tests tools/database
python -m pytest tests/business tests/domain tests/security tests/persistence/test_unit.py -q --tb=short
$env:LABSAFE_TEST_REDIS_SERVER = '<本机 redis-server 可执行文件>'
python tools/database/run_mysql_tests.py --mysqld '<本机 mysqld 可执行文件>'
python -B tools/design/build_specs.py --check
python -B tools/design/test_readiness_design.py
python -B tools/design/validate_specs.py
~~~

设计检查使用独立工具依赖环境。数据库启动器只创建随机端口、随机凭据、独立临时目录中的可丢弃 MySQL；Redis fixture 同样核对自建子进程 PID，不接管、清空或关闭已有服务。CI 仍复用现有持久化测试入口。

### 5.2 本地成绩

| 检查 | 实际结果 |
|---|---|
| 累计单元/HTTP边界/领域/安全/持久化单元 | 691 passed；相对 I-02C 新增 79 项，其中领域 47 项、HTTP 边界 32 项 |
| 隔离持久化套件 | 127 passed、无 skipped；相对 I-02C 新增 34 项，最终完整执行退出码 0 |
| 真实数据库范围 | MySQL 8.0.33、0001_initial、43 表、115 外键，结构 errors=[]；127 项中 106 项为真实 MySQL 用例，21 项为重复的持久化单元用例 |
| 静态检查 | Ruff check 全部通过；format --check 为 82 个 Python 文件通过 |
| 进程与协议 | 业务环境协议 4 项通过；独立 AI 环境 fixture 15 项、协议 4 项通过；业务进程 smoke 已计入 691 项 |
| 契约与文档 | 16 份生成物无漂移、148 项 synthetic 就绪检查、完整设计校验 PASS、2 份 OpenAPI、207 个文档链接 |
| 实际路由与 Git | 实际注册 26 个公共业务 operationId；git diff --check 通过；暂存区为空、未提交/Push/新 PR |

本轮为 2026-09-30、Python 3.11.4 的实际本地执行结果。两个主套件重叠 21 个持久化单元用例，不能直接将 691 与 127 相加作为唯一测试数；协议用例也在业务/AI 两环境重复验证。未运行远程 CI、真实 GPU 或完整生产部署。

本批覆盖父类型矩阵/归档父节点、完整创建链路及公开响应 Schema、普通角色/无角色/跨租户/跨实验室拒绝、授权后统计、分页同时间排序与 COUNT 后并发插入的快照一致性、模板重复字段/规范化冲突、发布/克隆版本与审计原因、100 项容量、子项/审计故障全回滚、三类资源同 key 十并发、同/不同源 revision 的四会话并发克隆、四会话同步认证、预检后撤会话拒绝提交、撤权后重放拒绝及 Redis 写失败封闭。

测试入口为 `tests/domain/test_foundations.py`、`tests/business/test_foundation_boundary.py`、`tests/persistence/test_foundations_mysql.py`；已有身份/角色/结构/安全套件同时回归。首次会话锁竞争按 4.2 修复；扩展测试另修正了测试夹具误用 NullPool 不存在的 checkedout 接口，改为直接跟踪应用事务活动数，未改弱事务外限流断言。最终所有 127 项重新完整通过。保留第三方 Starlette/AnyIO 已有弃用警告，未擅自升级依赖。

## 6. 接入、剩余工作和回退

沿用[开发指南 3.6](../../DEVELOPMENT.md)的 dev/test、API_IDENTITY_ENABLED=1、PUBLIC_ORIGIN、CSRF_KEY_FILE 与专用 MySQL/Redis，无新增开关、依赖或迁移。默认不开启；未配置的基础接口明确 503。/ready 仍仅是局部数据库版本及 Redis 检查，不声称对象存储、完整业务、Worker 或真实模型就绪。

浏览器仍需同源 HTTPS、Secure session cookie；写请求需要 Origin、JSON、单个 X-CSRF-Token 和 Idempotency-Key。模板创建正文上限为 2 MiB，以容纳合法 100 项完整 Unicode/转义 JSON；其他本批小请求仍为 64 KiB。超过该临时开发请求预算按既有 VALIDATION_ERROR/422 拒绝，不借用专属图片的 IMAGE_TOO_LARGE 错误码；未来上传/import 单独实现契约。

调用顺序：管理员登录 → 建学院/实验室/room 和所需子位置 → 创建模板 → 用返回 version 发布 → 为采集用户授权 inspector 并重新登录 → 使用已发布 template_id 和同实验室 location_ids 创建巡检 → 查询草稿及 item_ids。前端完整采集 UI 尚未实现。

下一批优先补巡检项查询与服务端同源 allowed_actions，再实现上传 grant/complete 及对象版本/SHA 校验。巡检 complete/cancel 必须联动完整聚合、run/task fencing 与历史确认记录，不作为本批草稿创建的隐含能力。Outbox/Worker→独立 AI 属 I-03，D-FINE-N/GPU 属 I-ML，均未由本批放行。

开发回退可关闭 API_IDENTITY_ENABLED 并重启，停止身份及基础接口；不会删除已建组织/模板/巡检，不回拨版本，不恢复旧 session，也不删除审计。无新迁移，不需要改 schema。后续提交、Push、PR 需用户授权，遵守[Git 协作规范](../../CONTRIBUTING.md)，本轮安全修复需独立评审。
