
# LabSafe 开发指南（I-01A–I-02F2）


## 1. 本阶段边界

所有 Python 服务统一使用 Python 3.11.x；业务与 AI 使用独立虚拟环境和独立进程。
依据见 [运行时决策](docs/02-architecture/07-python-runtime.md)。前端 CI 使用 Node.js 20。


已提供真实 FastAPI/Celery 入口、同协议开发 fixture、配置保护，以及 I-01B 初始数据库迁移、租户初始化和真实 MySQL 测试。
I-01C 另提供会话、RBAC、幂等与租户事务原语；I-02A 增加 13 个纯领域命令守卫及正反例测试。
I-02B 接通 7 个认证/用户 HTTP API，以及对应应用事务、用户仓储、Redis 限流和真实 MySQL 并发验证。
I-02C 接通角色列表、授予、撤销 3 个 API，覆盖 scope、用户版本、全会话撤销与最后管理员并发保护。
I-02D 接通学院、实验室、位置、模板创建/发布/克隆及巡检草稿 16 个 API；可通过真实数据库完成组织→模板→草稿链路。
I-02E 接通巡检项列表、详情 2 个 API；提供同源守卫探测基础，未实现的写动作不显示为可执行能力。
I-02F1 新增可选 createUpload；实现上传授权、权限/额度守卫、真实 SDK 本地签名和固定版本读取/SHA 适配基础。
I-02F2 新增 completeUpload/getImage，累计 31 个公共业务 API；上传完成只受理为 validating，并原子登记验证任务/TaskDispatch。图片下载、图像验证消费及 ready 提交仍未实现。
尚不包含完整巡检业务仓储/API、持久任务闭环、真实图像处理、训练权重或生产部署。

未开启身份 API 时，/ready 只检查 I-01A 进程配置；开启后还检查迁移版本和 Redis 连通性，但仍不是完整系统就绪。开启上传也不会使 /ready 检查真实桶、IAM、签名请求或图像验证。
所有后端进程和前端构建均拒绝 production；AI_MODE 只接受 mock。

## 2. 安装依赖

在仓库根目录执行。Windows 示例：

~~~powershell
py -3.11 -m venv .venv-business
.\.venv-business\Scripts\python.exe -m pip install -r requirements/dev.txt

py -3.11 -m venv .venv-ai
.\.venv-ai\Scripts\python.exe -m pip install -r requirements/dev-ai.txt
~~~

Linux 使用 python3.11 创建环境，并将 Scripts/python.exe 替换为 bin/python。
不需安装模型、CUDA、数据库驱动到 AI 环境；AI 只安装 common.txt 与测试依赖。
requirements 是顶层依赖约束，不是完整传递依赖锁或 GPU 兼容性证明。

## 3. 配置和启动

.env.example 是变量清单，当前程序不会自动读取 .env。请显式设置环境变量。
以下每个终端均从仓库根目录启动，不能把三个前台进程串在同一个终端等待。

### 3.1 API 终端

~~~powershell
$env:APP_ENV = 'dev'
.\.venv-business\Scripts\python.exe -m apps.api.run
~~~

API 监听 127.0.0.1:8000；GET /health 为存活，GET /ready 明确返回当前就绪范围。
默认 API_IDENTITY_ENABLED=0；认证/用户路径返回 503，不提供假的登录成功。真实身份 API 的启用步骤见 3.6。

### 3.2 AI 终端

首次创建本地随机令牌；文件在 .local-secrets/ 下，受 Git 忽略：

~~~powershell
.\.venv-ai\Scripts\python.exe -c "import pathlib,secrets; p=pathlib.Path('.local-secrets/ai-token'); p.parent.mkdir(exist_ok=True); p.write_text(secrets.token_urlsafe(32),encoding='ascii')"
$env:APP_ENV = 'dev'
$env:AI_MODE = 'mock'
$env:AI_TOKEN_FILE = (Resolve-Path .local-secrets/ai-token).Path
$env:AI_ALLOWED_TENANTS = '11111111-1111-4111-8111-111111111111'
$env:AI_FIXTURE_SCENARIO = 'no_targets'
.\.venv-ai\Scripts\python.exe -m apps.ai_inference.run
~~~

AI 监听 127.0.0.1:8001。五个接口均使用 /internal/inference/v1 前缀，并要求 Bearer 服务令牌：

| 方法 | 路径 | 开发行为 |
|---|---|---|
| GET | /health | supervisor HTTP 进程存活与当前 attempt |
| GET | /ready | 生命周期启动成功才 200；忙碌不等于未就绪 |
| GET | /version | development、is_simulated、fixture 身份与真实文件摘要 |
| POST | /quality | InferenceRequest → InferenceResult；通过或重拍分支 |
| POST | /runs | 同协议；无目标返回 needs_review，不冒充安全通过 |

不再提供 /fixture、裸 /version 或未鉴权的 AI /healthz。
请求和响应按生成的契约校验，不给 InferenceResult 私加 is_simulated 字段；模拟身份来自 /version，后续由 Worker 固化到业务 run。

AI_ALLOWED_TENANTS 必须是显式 UUID 清单；令牌为至少 32 字节的随机 ASCII 字符串。
请求模型/词典版本、实际 fixture 文件 SHA、设备 cpu、request_hash、图片关联与 analysis key 必须匹配。
fixture 不下载对象或读取图片；质量分数、空事实与阶段耗时均是合成值，不是性能或识别证据。

AI_FIXTURE_SCENARIO 可设 no_targets、needs_retake、error、timeout；AI_FIXTURE_DELAY_MS 范围 0–10000。
这些只能由开发者在服务启动环境配置，不能通过请求选择。
同一实例只接收一个在途请求；重复活动 attempt 返回 409，其他请求返回 429；超时返回 504 并释放容量。
当前模拟延迟可协作取消；真实 spawn 计算子进程及 kill/join 恢复在 I-03 实现，不能把本次 fixture 当成已完成的生产 supervisor。

开发 fixture 位于 apps/ai_inference/fixtures/：model.json、dictionary.json 均为小型合成文件；runtime-lock.json 校验 Python minor 及直接依赖版本。
该锁的 scope 明确为 fixture-direct-constraints-only，不声称传递依赖可复现，也不能用于真实模型激活。

### 3.3 Worker 终端

无需 Redis 的入口检查：

~~~powershell
$env:APP_ENV = 'dev'
.\.venv-business\Scripts\python.exe -m apps.worker.run --check
~~~

实际 Celery 队列运行按设计使用 Linux/WSL 的独立业务环境及 Redis：

~~~bash
APP_ENV=dev REDIS_URL=redis://localhost:6379/0 .venv-business/bin/python -m apps.worker.run
~~~

--check 只验证真实 Celery 配置与任务注册，不证明 Redis 连通或任务消费。
labsafe.fixture.echo 只是本地 smoke 任务，不是推理流程，不能用它绕过持久任务或写入成功状态。
完整 Worker→AI→数据库链路及 Outbox/inbox/fencing 在 I-03 落地。

### 3.4 Web 终端

~~~powershell
$env:APP_ENV = 'dev'
$env:VITE_APP_ENV = 'dev'
Set-Location apps/web
npm ci
npm run dev
~~~

两个环境变量必须相等且为 dev/test。开发页面始终显示“开发环境，结果不用于安全判断”。
构建命令是 npm run build；production 或不一致环境必须失败。


### 3.5 数据库迁移与初始化（I-01B）

先在独立的开发 MySQL 8.0.16+ 实例创建专用空库 labsafe_dev_local。
由数据库管理员提供仅对该库具有 SELECT/INSERT/UPDATE/DELETE/CREATE/ALTER/DROP/INDEX/REFERENCES 权限的迁移/初始化账户；不要给 API/Worker 或 AI 使用该管理账户。
本阶段只允许本机回环连接；远端 TLS、应用最小权限账户和生产发布不在 I-01B 范围内。

将管理员提供的 mysql+pymysql 连接 URL 保存到 .local-secrets/database-url，包含用户名、URL 编码后的密码、回环地址、端口和准确的库名。
URL 不允许 query 参数；程序只读取 DATABASE_URL_FILE，不读取明文 DATABASE_URL，也不自动加载 .env。
该文件必须仅当前用户可读；不要把真实连接串放在命令行、终端历史、日志、PR 或文档中。

~~~powershell
$env:APP_ENV = 'dev'
$env:DATABASE_SCHEMA = 'labsafe_dev_local'
$env:DATABASE_URL_FILE = (Resolve-Path .local-secrets/database-url).Path
.\.venv-business\Scripts\python.exe -m packages.persistence.cli upgrade
.\.venv-business\Scripts\python.exe -m packages.persistence.cli verify
.\.venv-business\Scripts\python.exe -m packages.persistence.cli bootstrap-tenant --code my_org --name '机构名称' --timezone Asia/Shanghai --username admin --display-name '初始管理员' --reason '首次初始化'
~~~

最后一步交互式输入并确认 12–128 字符密码，不提供项目默认密码。
非交互环境使用 --password-file 指向一次性 UTF-8 秘密文件：内容就是密码，不要额外添加 BOM 或结尾换行，程序不会 trim/规范化密码；成功后由提供该文件的操作者安全移除。
用户名执行 NFKC+casefold；租户、六角色、租户级 safety_admin 授权与脱敏审计在一个事务中提交。
重复初始化同一租户返回错误，绝不重置已有密码；本命令不是登录接口，也不是管理员重置工具。
upgrade 成功不等于结构正确，必须继续 verify；JSON 中 errors 应为空，退出码为 0。
CLI 退出码：0=成功，2=输入/保护检查失败，1=数据库/迁移/结构检查失败；错误不输出连接串或密码。

初始迁移仅允许空库（可以已有 Alembic 版本表）；不使用 stamp 绕过保护。
MySQL DDL 非整体事务：中途失败可能遗留部分表，不能承诺自动回滚。
此时保留失败证据，并由操作者确认后丢弃整个专用测试库重建；不要对已有业务库执行重建。
降级会删除全部 43 张业务表，只能在 APP_ENV=test 且 DATABASE_SCHEMA 为 labsafe_test_ 前缀的可丢弃库执行：

~~~powershell
# 仅在已确认的专用测试库配置中执行，绝不能指向业务/开发数据库。
$env:LABSAFE_ALLOW_DESTRUCTIVE_DOWNGRADE = $env:DATABASE_SCHEMA
.\.venv-business\Scripts\python.exe -m packages.persistence.cli downgrade
Remove-Item Env:LABSAFE_ALLOW_DESTRUCTIVE_DOWNGRADE
~~~

本机自动验证不需要配置或连接已有库：

~~~powershell
$env:LABSAFE_TEST_REDIS_SERVER = 'D:/Tool/Redis/redis-server.exe'
.\.venv-business\Scripts\python.exe tools/database/run_mysql_tests.py --mysqld 'D:/SQL/MySQL/MySQL Server 8.0/bin/mysqld.exe'
~~~

把路径替换为实际 mysqld 可执行文件。脚本创建临时 data directory、随机回环端口和随机凭据，核对实例身份后才建库；结束后只关闭本次子进程并清理临时数据。
不注册或修改 Windows 服务。Linux 本机模式需已安装 mysqld，并以允许运行 mysqld 的非 root 用户执行；CI 使用隔离 MySQL 容器模式，不依赖本机服务。
I-02B 测试还需要 redis-server（位于 PATH 或上述显式路径）；测试夹具启动随机回环端口的独立 Redis 子进程并核对 PID，不复用或清空已有 Redis。CI 使用该 job 的隔离 Redis 容器。

### 3.6 开启身份管理和基础业务 API（I-02B/C/D/E）

先完成 3.5 的专用 dev/test 数据库迁移、verify 和租户初始化。API 使用该库的独立运行账户，只授予运行所需的 SELECT/INSERT/UPDATE/DELETE，不复用具有 DDL 权限的迁移账户。将该运行账户连接串放入仅当前用户可读的 `.local-secrets/api-database-url`；仍要求回环地址、准确库名、mysql+pymysql、无 URL query 参数。

首次生成独立 CSRF 秘密文件；已存在时不要重建，否则现有会话会失效。以下命令使用排他创建，意外覆盖会失败。

~~~powershell
.\.venv-business\Scripts\python.exe -c "import pathlib,secrets; p=pathlib.Path('.local-secrets/csrf-key'); p.parent.mkdir(exist_ok=True); f=p.open('xb'); f.write(secrets.token_bytes(32)); f.close()"
$env:APP_ENV = 'dev'
$env:API_IDENTITY_ENABLED = '1'
$env:DATABASE_SCHEMA = 'labsafe_dev_local'
$env:DATABASE_URL_FILE = (Resolve-Path .local-secrets/api-database-url).Path
$env:CSRF_KEY_FILE = (Resolve-Path .local-secrets/csrf-key).Path
$env:PUBLIC_ORIGIN = 'https://labsafe.localhost:8443'
$env:REDIS_URL = 'redis://127.0.0.1:6379/0'
.\.venv-business\Scripts\python.exe -m apps.api.run
~~~

Redis URL 必须指向操作者已准备好的专用开发实例；上例是变量格式，不代表程序会创建或隔离该实例。不要使用生产/共享 Redis。CSRF 文件是原始随机字节而不是待解码的 base64，至少 32 bytes，只给 API 读取，不能复用 AI bearer 令牌。

另行配置本地 HTTPS 反向代理及可信开发证书：在同一 `PUBLIC_ORIGIN` 提供前端，并将 `/api/v1`、`/health`、`/ready` 转发到 `127.0.0.1:8000`。PUBLIC_ORIGIN 必须与浏览器 Origin 精确一致，不含尾斜线、路径或凭据。当前仓库未提供反代配置或登录 UI，本批验证采用 HTTPS base URL 的 TestClient，不声称已完成真实浏览器 TLS 联调。不能把 Cookie 改为不安全模式来绕过 HTTPS。

登录 `POST /api/v1/auth/login` 发送 JSON `{tenant_code,username,password}` 与 Origin；后续请求携带服务端 session cookie。`GET /api/v1/me` 获取 CSRF；创建用户/禁用/退出还需单个 X-CSRF-Token、Idempotency-Key、Origin 和 application/json。具体字段、响应和错误使用 [公共契约](contracts/public-api-v1.yaml)，不要将密码或 cookie 粘贴到日志/PR。

本阶段不信任 X-Forwarded-For；反代后的 IP 限流按直接 peer 计数，不声称已识别真实终端 IP。身份及基础小请求暂限 64 KiB，模板创建为 2 MiB，以容纳 100 项完整 Unicode/转义正文；这不是未来上传/import 限额。/ready 只核对 `0001_initial` 与 Redis ping；模式结构仍必须用 CLI verify，存储、Worker、完整业务流程和真实模型均未纳入该门禁。生产/远端数据库仍被拒绝。

### 3.7 组织→模板→巡检草稿（I-02D）

共用 3.6 的开关和安全请求头；默认未配置时返回 503，不使用内存数据伪造成功。按下列顺序接入，字段完整约束见 [I-02D 验收](docs/08-delivery/13-i02d-foundation-api.md)及公共契约：

1. 管理员创建学院 `{name,code}`，以返回 id 创建实验室 `{college_id,name,code}`。
2. 向 `/laboratories/{id}/locations` 创建 room：`{parent_id:null,type:"room",label}`；area 引用 room，shelf/cabinet 引用 room 或 area。
3. 创建模板 `{name,items}`，每项必须含 `{id,code,title,capture_hint,sort_order,required}`；items 为 1–100，id/code/sort_order 分别唯一。
4. 向 `/templates/{id}/publish` 发送 `{expected_version,reason}`，version 使用刚查询/创建得到的源模板 version；发布后不能修改正文。clone 接收同一命令结构，生成同 family 下一 revision 的新 draft/新子项 ID。
5. 授予目标用户实验室 inspector 后让其重新登录；以 `{laboratory_id,template_id,location_ids}` 创建巡检。模板必须 published，位置必须同实验室 active，子项×位置≤100。
6. `GET /inspections/{id}` 返回草稿和真实 item_ids；巡检项查询见 3.8，可选上传与验证受理见 3.9/3.10。尚无图像验证消费/执行推理/巡检完成或取消接口，不将草稿呈现为已完成巡检。

新意图使用新 Idempotency-Key；网络重发保留 key 和原正文。状态/版本冲突由客户端刷新后重新决定，不静默覆盖。学院/模板列表不接收 laboratory_id；实验室/巡检列表按当前授权实验室过滤后再分页计数。

### 3.8 巡检项列表与详情（I-02E）

携带登录后的 Secure session cookie，使用 `GET /api/v1/inspections/{inspection_id}/items` 查询列表，支持 page/page_size/laboratory_id；`GET /api/v1/inspection-items/{item_id}` 查询详情且不接受 query。读请求无需写用 CSRF 或 Idempotency-Key，错误仍为统一 envelope，所有响应 no-store。

列表先验证父巡检范围再计数；不存在/越权父巡检为 404，可见但无子项为 200 空页。laboratory_id 越权或不存在为 404，可见但不匹配父巡检实验室为 200 空页。COUNT 与页数据为单请求同一 RR 快照，不承诺跨页冻结。

当前所有状态的 `allowed_actions=[]`：未接入 submit/retry/complete 巡检项写路由，不根据 status 伪造按钮；createUpload 不改变这一能力集。领域探测复用 submit/retry/complete 原守卫，但完整可信上下文加载器和对应命令事务尚未接入；不能只改能力开关放行。精确边界与后续关闭清单见 [I-02E 验收](docs/08-delivery/14-i02e-item-query-api.md)。

### 3.9 可选上传授权（I-02F1）

本节说明 `POST /api/v1/uploads`；同一开关也控制 3.10 的完成/查询接口，默认 `API_UPLOADS_ENABLED=0`。先安装当前业务 requirements（含 boto3），完成 3.6 的身份/数据库/Redis 配置；不要给 AI 环境安装对象存储或数据库依赖。

启用前由操作者另行准备私有、已开启版本化的 `labsafe-private` 桶、staging 生命周期、API 最小权限账户和两份仅 API 可读的凭据文件。不要挂载 MinIO 管理员或 cleanup 专用凭据。仓库尚未提供完整部署/IAM 自动配置，以下变量不是已完成真实存储联调的证明。

~~~powershell
$env:API_UPLOADS_ENABLED = '1'
$env:PUBLIC_ORIGIN = 'https://labsafe.localhost'
$env:S3_PUBLIC_ENDPOINT = $env:PUBLIC_ORIGIN
$env:S3_ENDPOINT = 'http://127.0.0.1:9000'
$env:S3_BUCKET = 'labsafe-private'
$env:S3_REGION = 'us-east-1'
$env:S3_ACCESS_KEY_FILE = (Resolve-Path .local-secrets/s3-api-access-key).Path
$env:S3_SECRET_KEY_FILE = (Resolve-Path .local-secrets/s3-api-secret-key).Path
.\.venv-business\Scripts\python.exe -m apps.api.run
~~~

上传规范要求公共同源 HTTPS **443**；3.6 仅身份示例的 `:8443` 不能直接用于上传。必须同步调整前端/反代证书和 Origin，按[部署规范 3.1](docs/07-quality-operations/02-deployment-operations.md)代理 `/labsafe-private/`，保持 Host、Content-Type、原始 path/query，PUT 限制 15 MiB，并关闭签名 query 日志。示例回环 9000 仅适用于受控本地开发，不得暴露公网或代替生产内网隔离。

凭据只从显式 secret 文件读取，不回退 AWS 环境凭据、profile 或 metadata。公共端点必须与 PUBLIC_ORIGIN 字符串相同，无路径、尾斜线、query 或凭据；不符合则启动失败。文件和 URL 不写入 Git、终端共享记录或日志，`.env` 仍不自动加载。

1. 登录并取得 CSRF，用 3.7/3.8 返回的可采集 item ID 请求 grant；携带 session cookie、Origin、X-CSRF-Token、Idempotency-Key 和 application/json。
2. 正文为 `{owner_type,owner_id,filename,mime_type,size_bytes,sha256,captured_at}`。owner_type 为 inspection_item/remediation_task；文件 1–15 MiB、JPEG/PNG/WebP，SHA 是拟上传字节的 SHA-256，时间必须带 RFC3339 时区。
3. 201 返回 `{data:{upload_id,object_key,put_url,required_content_type,expires_at,max_bytes},request_id}`。在已配置并验收的代理上，用返回 URL PUT 原始文件字节和精确 required_content_type；不要用 multipart/form-data，不自行拼 key、改 Host 或签名 query。
4. 同用户独立 10 次/分钟 grant 限流，并最多 10 个未到期 granted；429 遵循 Retry-After。网络重发保持 key/正文，返回原 URL/到期时间，不续期；过期后新的上传意图使用新 key。

PUT 成功不等于图像有效或业务完成。后续完成/查询见 3.10，客户端不得自行标记 ready；固定版本 GET/SHA helper 尚未接入 Worker。授权的权限、锁序及该批历史测试见 [I-02F1 验收](docs/08-delivery/15-i02f1-upload-grant-api.md)。

回退设 `API_UPLOADS_ENABLED=0` 后重启，不影响既有身份/查询；不删 uploads/审计/幂等历史，也不通过数据库降级清空数据。已发出的预签名 URL 不因 API 开关关闭或用户会话撤销立即失效，按原 600 秒到期；实际应急凭据撤销必须另行评估，不承诺开关具备对象存储撤销能力。

### 3.10 上传完成受理与图像元数据（I-02F2）

沿用 3.9 的开关、依赖、secret 和同源配置，无新增迁移或启动变量。当前用于受控 dev/test 接口联调，不是可供业务使用的完整采集流程。

1. 取得 grant 并成功 PUT 后，向 `POST /api/v1/uploads/complete` 发送 `{upload_id,sha256}`；带 Origin、session cookie、X-CSRF-Token、Idempotency-Key 和 application/json，不传对象 key/version/owner 或 query。
2. 首次成功返回 202，`data` 是 Image：`status=validating`，analysis_sha256/width/height 均为空。内部 HEAD 的准确版本被固定；image、validate_image 任务、初始 TaskDispatch、upload 状态、审计和幂等响应在同一事务提交。既有巡检项仍为 draft，不能按受理结果放行提交。
3. 用 `GET /api/v1/images/{id}` 读取元数据；需要当前 owner.read 权限，无写用 CSRF/幂等头，不接 query。只返回契约字段，不返回内部 key/version、签名 URL、凭据或任务 payload。
4. 同 key/正文重发返回原响应和 request_id；新 key 对已完成 upload 返回同一图像的当前元数据，不再 HEAD、不另建任务。原 grant 过期或 owner 状态推进不重复执行首次创建前置，但始终重验当前权限；不同 hash 不可更换原图。

首次完成要求 grant 未过期、owner 仍可采集、请求 hash 与 grant 声明一致、HEAD 大小/MIME 匹配。HEAD 元数据中的 SHA 不可信；validating 时的 original_sha256 仍为声明值，真实字节 SHA/解码由后续 Worker 验证。文件被重新 PUT 不会改写已固定的版本。

本批没有 publisher/sweeper/验证 consumer，任务停留在数据库 ready、图像停留 validating，不应持续轮询期待本版本自行完成。staging 的 24 小时生命周期仍可能使未消费输入过期；启用真实验证消费前仅使用可丢弃合成测试对象，不能积压实际业务证据。关闭 API 开关不删除已登记任务；没有实现后台取消或清理功能。

验收、精确重放语义及后续关闭清单见 [I-02F2 验收](docs/08-delivery/16-i02f2-upload-completion-api.md)。真实 IAM/MinIO/nginx/TLS 联调仍待完成，TestClient/Stubber 不替代这些证据。

## 4. 验证

根目录、业务环境执行：领域守卫也使用业务环境，不向 AI 环境引入业务授权或持久化依赖。

~~~powershell
.\.venv-business\Scripts\python.exe -m unittest discover -s tests/business -t . -v
.\.venv-business\Scripts\python.exe -m unittest discover -s tests/protocol -t . -v

.\.venv-business\Scripts\python.exe -m pytest tests/persistence/test_unit.py --tb=short
.\.venv-business\Scripts\python.exe -m pytest tests/business tests/domain tests/security -q
.\.venv-business\Scripts\python.exe -m ruff check apps packages tests tools/database
.\.venv-business\Scripts\python.exe -m ruff format --check apps packages tests tools/database

~~~

AI 环境执行：

~~~powershell
.\.venv-ai\Scripts\python.exe -m unittest discover -s tests/ai -t . -v
.\.venv-ai\Scripts\python.exe -m unittest discover -s tests/protocol -t . -v
~~~


I-01A 的业务/AI/协议测试自行在临时目录生成令牌和合成请求，不要求预设 APP_ENV，也不连接 Redis/MySQL/对象存储。
包含实际 API/AI 子进程 HTTP smoke，不只是 import 非空检查。
I-01B–I-02F2 的真实数据库测试必须运行 3.5 的隔离启动器；直接 pytest tests/persistence 会跳过未配置的 MySQL 用例，不能把跳过算作数据库通过。上传测试的本地 SDK 签名/Stubber 不连接真实 S3，也不能替代 UP-03/SEC-03 部署验收。


设计校验使用独立工具环境，安装 tools/design/requirements.txt 后执行：

~~~powershell
python -B tools/design/build_specs.py --check
python -B tools/design/test_readiness_design.py
python -B tools/design/validate_specs.py
~~~

修改契约时只改 tools/design 的生成源，再运行 build_specs.py；同一生成器输出 contracts 和运行时 packages/inference_protocol/contract.json。
CI 必须先 --check，不能先生成来掩盖漂移。设计校验不是应用、模型或部署验收。

## 5. 提交和下一阶段

遵循 [Git 协作规范](CONTRIBUTING.md)。提交特性分支、创建 PR；不直接推送 main，不自行合并或代替独立审查。
不提交虚拟环境、.local-secrets、真实图片、权重或 .env。

I-01B 的实际结果与后续边界见 [验收记录](docs/08-delivery/08-i01b-acceptance.md)。I-01C 会话、RBAC、幂等与租户事务实现及验收范围见 [I-01C 验收记录](docs/08-delivery/09-i01c-acceptance.md)。

截至 2026-10-01，I-01A PR #5、I-01B PR #7、I-01C PR #8 均已合并。本轮基于 #8 合并后的 main 进入 I-02；数据库/安全变更仍需独立评审，不直接推送 main。
I-02A 的领域守卫边界见 [领域命令守卫验收](docs/08-delivery/10-i02a-domain-guards.md)；本批最新身份 API、事务和故障验证见 [I-02B 验收](docs/08-delivery/11-i02b-identity-api.md)。本批未推送，不能将旧 PR 的 CI 成绩作为新分支已通过。
I-02C 的角色接口、作用域、版本与会话语义见 [角色 API 验收](docs/08-delivery/12-i02c-role-api.md)。角色请求的 expected_version 来自目标 User.version，laboratory_id 必须显式传 UUID/null；成功改权会撤销目标全部会话，包括操作者修改自身角色时的当前会话。三接口与 I-02B 共用开关和安全请求头，无新增配置。
I-02D 的接口、事务与本轮会话锁修复见 [基础 API 验收](docs/08-delivery/13-i02d-foundation-api.md)。实施计划的 I-0 表格同步记录本地验证状态，不把未提交工作记作远程 CI 或独立批准。
I-02E 的两个巡检项查询、同源探测基础与能力门禁见 [巡检项查询验收](docs/08-delivery/14-i02e-item-query-api.md)；这不代表完整写动作投影已接通。
I-02F1 的上传授权、S3 配置/签名、固定版本适配与实际测试见 [上传授权验收](docs/08-delivery/15-i02f1-upload-grant-api.md)，默认关闭，仅 dev/test；CI 的 AI 依赖隔离清单同时禁止 boto3/botocore，本地通过不代表远程 CI 已运行。
I-02F2 的完成受理/图像查询和原子任务写入见 [完成受理验收](docs/08-delivery/16-i02f2-upload-completion-api.md)；不把初始 task/dispatch 记录等同于持久任务闭环。
下一批优先 I-03A：落地 publisher、任务领取/lease/fencing、重试和 sweeper 共用基础，再由 I-02F3 接入 general 图像验证消费及真实存储联调；此前不标 ready。后续接提交/重试/完成及事实、评估、复核与整改完整事务。真实模型主线按 I-ML-01 推进，不以基础 API、查询或纯领域测试代替模型和生产验收。

