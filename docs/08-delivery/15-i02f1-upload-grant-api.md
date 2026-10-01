# I-02F1 上传授权与 S3 适配基础验收

日期：2026-10-01。状态：本地实现及验证完成；仅 dev/test，未提交、Push 或创建新 PR。历史 I-02A–E 成绩保持原记录，不以本批局部通过替代整个 I-02。

## 1. 本批交付边界

| 层次 | 本批交付 | 明确不包含 |
|---|---|---|
| 公共 API | POST /api/v1/uploads，operationId=createUpload，成功 201 | completeUpload/getImage、图像下载/删除、巡检项提交/重试/完成 |
| 领域/应用/仓储 | owner 采集授权、元数据守卫、双额度、原子 grant/审计/幂等 | image 创建、owner 状态变化、validate_image 任务或 Outbox |
| S3 适配 | 真实 Boto3 SDK 本地 SigV4；内部 HEAD、固定 VersionId GET 和流式 SHA helper | 真实 MinIO/IAM/反代联调、图像解码/EXIF/分析图、ready 判定 |
| 配置/依赖 | 默认关闭的上传开关；业务环境独立 secret/SDK；AI 依赖隔离门禁 | 生产启用、完整传递依赖 lock、存储/任务就绪探针 |

累计注册 29 个公共业务 operationId；新增只有 createUpload。默认 `app.state.uploads=None`，合法上传授权请求在未配置时返回 503。`allowed_actions` 仍为空，不能由 grant 的存在推导提交能力。

## 2. 接口与错误

### 2.1 请求和响应

请求遵循[公共契约](../../contracts/public-api-v1.yaml)：

| 字段 | 输入及语义 |
|---|---|
| owner_type / owner_id | inspection_item 或 remediation_task；UUID 经 HTTP 统一规范化后由领域校验，不接收客户端 tenant/lab |
| filename | 1–255 字符展示名；拒绝空白、路径分隔符、控制字符及单独 . / ..；不参与对象 key |
| mime_type | image/jpeg、image/png、image/webp |
| size_bytes | 严格整数，1–15728640（15 MiB），不是图片尺寸或实际内容验证 |
| sha256 | 拟上传原始字节的 64 位小写十六进制 SHA-256；当前仅登记声明 |
| captured_at | 带显式时区的 RFC3339，转换为 MySQL 可存储的 UTC 毫秒；无另加的新鲜度、未来窗口或小数位数上限 |

只允许这七个字段，不接收 query。继承已实现的同源 Origin、Secure session、单值 CSRF/Idempotency-Key、JSON 和请求体大小保护；所有响应 no-store。客户端拍摄时间及 SHA 声明都不能作为已经核验的证据。

201 响应为 `{data:{upload_id,object_key,put_url,required_content_type,expires_at,max_bytes},request_id}`。服务端生成 upload UUID 与 `staging/{tenant_id}/{upload_id}`，PUT URL 只指向该对象、包含 Content-Type/Host 签名，有效期 600 秒。expires_at 由 SDK 的 X-Amz-Date + X-Amz-Expires 精确推导，不能由另一次取时延长。

### 2.2 错误边界

- 401：会话不存在、失效或预检后被撤销；403：同源/动作或创建者/受派人不足；404：owner 不存在、跨 tenant/lab 或不可见。
- 409：owner 当前状态不接受新采集，或既有幂等冲突/请求执行中。重放不重新执行已完成命令的状态前置，但仍重验当前身份及 owner 权限。
- 422：HTTP Schema/类型/枚举/大小声明越界、未知字段/query、文件名/时间/SHA 等非法。HTTP Schema 已拦截的 MIME/大小声明错误为 422；不是实际对象内容 413/415。
- 413/415：沿用请求体大小/媒体类型保护；存储 helper 对实际超大对象或不支持的对象 MIME 分别返回 IMAGE_TOO_LARGE/UNSUPPORTED_MEDIA_TYPE，但这些 helper 尚未暴露为 complete API。
- 429：普通写频率、上传专属频率或未完成 grant 配额；包含 Retry-After。503：未配置、Redis 故障、签名/存储依赖故障。底层异常不泄露 endpoint、凭据、SQL 或 SDK 原始消息。

## 3. 权限、配额与事务

### 3.1 可信 owner 守卫

owner 从当前事务的数据库加载，不能由请求构造。

| owner | 权限和归属 | 新授权允许状态 |
|---|---|---|
| inspection_item | 当前实验室 capture 权限；原采集创建者或 safety_admin | 父巡检 draft/in_progress；子项 draft/uploaded/needs_retake/needs_review/failed |
| remediation_task | 当前实验室 assignee 权限，且确为当前受派人 | in_progress/rejected |

Task 不把 admin 等同受派人；合法整改证据也不因为父巡检已 completed 被阻断。数据库测试使用明确标注的合法外键合成整改历史，不代表整改命令链已实现。

### 3.2 两种额度不可互换

1. 预检结束后，在数据库事务外执行既有普通写限流和独立上传授权 10 次/分钟限流。Redis 故障拒绝请求；幂等重放也经过限流，不是绕过速率限制的通道。
2. 提交事务内，同 tenant/user 的 `status='granted' AND expires_at>now` 最多 10 条。已过期但尚未扫成 expired 的行不占额度，validating/ready/rejected 等状态也不计作未完成 grant。
3. 达容量上限时不写新记录，Retry-After 按最近到期时间向上取整且至少 1 秒；仅是重试提示，不预留下一次名额。

当前未新增配额专用复合索引，未验收大规模历史数据下的扫描/锁竞争性能；后续索引优化必须通过新迁移，不改写 `0001_initial`。

### 3.3 提交顺序与幂等

应用入口 `UploadGrantApplication.create` 先校验正文，结束短事务认证/CSRF 预检，再执行上述 Redis 限流。提交事务顺序固定：

1. 通过 session 定位 tenant/user，仅用于锁范围；取得 tenant 共享锁。
2. 取得当前 user 排他锁作为配额 mutex，必须早于认证或幂等外键所需的 user 共享锁。
3. claim 幂等记录，再重新认证/CSRF；不能把预检当成提交授权。
4. 加载并锁 owner：Item 按 inspection→item；Task 锁 remediation_task；校验范围、权限和新意图状态。
5. 已完成的同 key/正文返回原 HTTP 状态、原字节响应、request_id、URL 和 expires_at，不再预留/审计或续签。
6. 新意图用显式静态凭据进行本地 SDK 签名；以当前锁定读检查 grant 额度，然后 INSERT uploads、追加审计、保存幂等响应并原子提交。

用户 mutex 先于共享认证是本命令的必要例外，避免多会话 S→X 升级死锁；不能将其他请求的认证改为一律排他。配额必须是当前读，避免等待 mutex 后沿用 RR 旧快照。完整事务遇死锁最多尝试 3 次，每次重验，不能只重跑最后一条 SQL。

uploads/审计/幂等响应任一步失败均回滚。不创建 image/task/outbox，不递增 item/owner version 或改状态。原授权过期后同 key 仍返回原过期响应；新的上传意图用新 key。预签名 URL 是短期 bearer 能力，后续撤会话/关闭 API 不立即撤销已发 URL，仍依原到期时间和存储凭据策略约束。

## 4. 存储适配与配置

### 4.1 SDK 与网络边界

`packages/storage/s3.py` 使用业务环境 `boto3==1.43.106`。这是已安装验证的顶层版本约束，不是完整传递 lock；本批未给 AI 安装 SDK、ORM、Redis 或真实模型。

显式 secret 文件提供静态凭据，隔离 SDK 默认 config/credentials 文件与 profile，不使用环境凭据、metadata 或默认凭据刷新。两个 client 共用 path-style、SigV4、region us-east-1，分别用于公共端点本地签名和内部对象操作；连接/读取超时 2/5 秒、最多 2 次尝试，不继承代理配置。

`presign_put` 是无网络的本地密码学，因此可随响应在短事务内生成；测试禁止客户端发送网络请求并验证真实 SDK 签名结构。它不检查真实 IAM、桶存在性或版本化。HEAD/GET/PUT/Copy 等实际对象操作必须在数据库事务外，后续不得向事务内的 presign 路径加入凭据刷新或网络探测。

### 4.2 固定版本 helper

- `inspect_staging(tenant, upload)`：内部 HEAD 服务端构造的 staging key；要求非空、非 `null` VersionId，检查实际大小/MIME。不信任 Metadata 中的 SHA 或 ETag 作为真实摘要。
- `verify_staging_hash(tenant, upload, pinned, expected_sha256)`：只接受匹配的 staging key 与固定 VersionId，GET 显式传 VersionId；核对返回版本/长度/MIME，按 64 KiB 分块、最多 15 MiB 重算 SHA-256，所有分支关闭流。
- 固定版本不存在返回 OBJECT_NOT_FOUND，绝不回退 latest；截断/超长/回包版本变动/摘要不符均拒绝。SDK 依赖错误脱敏为 503。
- 以上是后续 complete/general 的适配基础，尚未连接公共完成接口或 Worker。即使 SHA 匹配，也没有完成解码、像素限制、方向归一、EXIF 清理、O/A 生成或 image ready。

### 4.3 开启与回退

执行[开发指南 3.9](../../DEVELOPMENT.md)；新增变量见 `.env.example`：API_UPLOADS_ENABLED、S3_ENDPOINT、S3_PUBLIC_ENDPOINT、S3_BUCKET、S3_REGION、S3_ACCESS_KEY_FILE、S3_SECRET_KEY_FILE。上传开关默认 0、只接受 0/1；启用必须有身份服务，配置失败拒绝启动并关闭本次分配的资源，正常退出同样释放资源。

公共端点必须精确等于 PUBLIC_ORIGIN，同源 HTTPS:443、无路径/query/凭据；bucket 固定 labsafe-private。身份示例 `:8443` 不能直接用于上传。操作者还须按[部署规范](../07-quality-operations/02-deployment-operations.md)准备私有版本化桶、最小权限 API 凭据、staging 生命周期、15 MiB 反代限制和签名 query 日志保护；不得给 API 挂管理员/cleanup 凭据。

`/ready` 不执行 S3/IAM/真实签名请求，也不证明存储或图像处理就绪。回退只关闭 API_UPLOADS_ENABLED 后重启；不删 grant/审计/幂等历史，不降级数据库，不声称关闭开关会立即撤销已发 URL。

## 5. 本地验证证据

### 5.1 执行命令

业务 Python 3.11 环境，从仓库根目录执行：

~~~powershell
python -m pip check
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

设计命令使用独立校验依赖；AI fixture/协议另在独立 AI 环境运行。隔离数据库使用 Git 忽略的 D 盘 `.pytest_cache` 下随机目录和进程级 TEMP/TMP，不改系统环境、不接管已有 MySQL/Redis 服务。不得把未配置数据库而 skipped 的结果当成通过。

### 5.2 实际结果

| 检查 | 本地结果 |
|---|---|
| 累计单元/HTTP/领域/安全/持久化单元 | 1227 passed；相对 I-02E 新增 266 项；保留 1 项既有 Starlette/AnyIO 弃用警告 |
| 上传专项隔离数据库 | 24 passed、154 deselected；只是专项结果，不能替代完整持久化回归 |
| 完整隔离持久化套件 | 178 passed、无 skipped、退出码 0；157 项真实 MySQL + 21 项重叠单元，本批新增 24 项数据库用例 |
| 数据库范围 | MySQL 8.0.33、0001_initial、43 表、115 外键、结构 errors=[]；未新增迁移/索引 |
| 静态检查 | Ruff check PASS；100 个 Python 文件 format --check PASS |
| 进程/协议与依赖 | 业务协议 4 项；独立 AI fixture 15 项、协议 4 项通过；业务 pip check PASS；AI 无 boto3/botocore/ORM/Redis/模型依赖 |
| 设计/文档 | 16 份生成物无漂移；148 项 synthetic readiness checks；完整设计 PASS、2 份 OpenAPI、224 个文档链接 |
| 路由/Git | 实际 29 个唯一公共业务 operationId，createUpload 存在，completeUpload 不存在，上传默认关闭；git diff --check 通过，暂存区为空 |

以上为 Python 3.11.4 的本地结果。主套件包含 21 项持久化单元，与完整持久化套件重叠，不能将两套成绩直接相加；两环境协议测试也重复。SDK 签名是真实本地计算，对象 HEAD/GET 使用 Stubber，不是实际 MinIO；MySQL/Redis 为真实隔离测试进程。

新增测试入口为 tests/domain/test_uploads.py、tests/business/test_upload_boundary.py、tests/business/test_s3_storage.py、tests/persistence/test_upload_grants_mysql.py，并扩展上传限流测试。覆盖 Item 144 格、Task 24 格授权/状态矩阵、严格元数据、额度/到期、签名/默认 profile 隔离、版本消失不降级、流关闭、SHA 错误、HTTP 安全边界与启动失败资源释放。

数据库专项覆盖 4 会话同 key/不同 key 竞争、10 个活跃 grant 上限、真实 Redis 10/min、过期释放额度、重放不续签/不重复审计、撤权/预检后撤会话及审计/幂等失败回滚。为独立检验数据库活跃额度，部分并发测试只替换上传速率调用；真实 Redis 上传速率另有独立用例，不能称所有并发测试完全无 mock。

### 5.3 本批复核修正和未验证项

复核关闭了显式默认 profile 在无配置文件时造成 ProfileNotFound 的问题：隔离 session 后移除 profile，而不是强制名为 default 的不存在 profile。上传配额锁前置于认证/幂等外键共享锁，避免 S→X 死锁；配额查询改用当前读，避免 RR 旧快照放宽额度。拍摄时间去掉公共契约未规定的 100 字符上限，增加长小数精度回归，仍按 UTC 毫秒入库。

尚未执行真实 MinIO/IAM/nginx/TLS 浏览器上传、图像解码、GPU/真实模型或远程 CI，无独立评审批准。UP-03/SEC-03 仍待真实环境证据，不能以本地 SDK、端点校验或 prefix/Stubber 测试关闭。生产保护保持不变。

## 6. 下一批可执行关闭清单

I-02F2 先完成上传验证受理，不跨越[持久任务](../02-architecture/05-durable-jobs.md)和[图像持久化](../02-architecture/06-data-persistence.md)边界：

1. 实现 completeUpload/getImage 的唯一路由/应用入口和白名单投影；重复同 upload 只生成一个 image/验证任务；仍重验 session、owner、scope、grant 期限及客户端/原 grant hash。
2. 在事务外执行 HEAD，固定 staging key/version；进入提交事务后重验所有可变前置条件。并发覆盖对象后仍使用已选定版本，删除版本不能回退 latest。
3. 将 upload/image 状态、validate_image task_runs、TaskDispatch/所需事件、审计和幂等响应原子提交；遵循稳定 logical_key、初始 generation/dispatch_sequence 及锁序。失败不得留下孤立 image 或先发 Redis 消息。
4. 增加真实 MySQL 同 key/不同 key 重复 complete、预检后撤权、HEAD 后状态变化、失效 grant、任务/Outbox 注入失败的回归。未有 consumer 时明确“受理/待验证”，不返回伪 ready。
5. 后续 general 消费按 lease/fencing 执行固定版本 GET/实算 SHA、解码/像素上限、方向归一/EXIF 清理和 O/A 对象生成，再原子提交 ready/rejected；补 Redis 丢失、崩溃重投和旧 token 回写拒绝测试。该闭环仍须 I-03 共用基础支持。
6. 另行执行真实最小权限 IAM、同源签名 PUT/固定版本 GET、过期/篡改拒绝、15 MiB 限制、私有桶/版本化及 URL 脱敏日志测试，留下 UP-03/SEC-03 证据后才关闭部署项。

不自动提交/Push/PR；后续按[协作规范](../../CONTRIBUTING.md)及用户授权推进，I-02、I-03、I-ML 和生产门禁均仍未完成。
