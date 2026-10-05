# I-02F4 受控图片下载与原图审计

日期：2026-10-02。范围：基于 F3 ready 图像，接通既有 downloadImage 契约的 analysis/original 分支，仅 dev/test。新增 1 个 API，累计注册 35 个公共业务 API。无新迁移、配置、前端或模型能力。位于 `wsq/i-03a-durable-dispatch`，本轮未提交、Push、创建 PR 或核验远程状态。

## 1. 接口与失败语义

GET `/api/v1/images/{id}/download`；默认 analysis，唯一 query 为单个 variant=analysis/original。UUID 规范化，拒绝重复/未知 query、客户端 tenant/key/version。返回现有 DownloadGrantResponse：`{data:{url,expires_at},request_id}`；不重定向，不返回原始对象字节。

| 情况 | 行为 |
|---|---|
| ready analysis | 当前图像实验室 READ；固定 A key/version，60 秒 GET 签名 |
| ready original | 先 READ，再额外 Admin；固定 O key/version，签发与敏感读取审计同事务 |
| 未认证/会话失效 | 401；不进入 SDK 签名 |
| 跨 tenant/lab、对象不存在 | 404；不暴露状态、key、version |
| 可见资源但缺少 Admin | original 为 403，analysis 不受此额外权限限制 |
| validating/rejected/deleted | 409 STATE_CONFLICT；无 staging GET、latest 回退或“失败原图”旁路 |
| 所选 O/A key/SHA/version 不完整或不规范 | 409 STATE_CONFLICT；不签任意 key |
| 签名配置/生成异常、无有效期限 | 503 DEPENDENCY_UNAVAILABLE，错误脱敏 |
| Redis 故障/额度耗尽 | 503/429；不使用普通 GET 的保守读降级；不签发 |
| 原图审计或 DB 提交失败 | 不返回 grant；事务回滚，内部异常脱敏 500，DB 不可用 503 |

共用 API_IDENTITY_ENABLED/API_UPLOADS_ENABLED、API S3 凭据和同源 HTTPS 配置；未配置为 503。GET 使用 session，不要求写命令的 CSRF/Idempotency-Key；每次原图签发独立审计，没有 grant 缓存。配置/使用/回退见 [开发指南 3.14](../../DEVELOPMENT.md)。

## 2. 实现入口

| 文件 | 职责 |
|---|---|
| [uploads.py](../../apps/api/app/uploads.py) | downloadImage 路由、strict query、no-store/no-referrer 响应 |
| [image_download.py](../../packages/application/image_download.py)（application） | preflight、事务外限流、事务内当前鉴权/签名/原图审计 |
| [image_download.py](../../packages/domain/image_download.py)（domain） | READ/Admin、ready、规范 O/A key/SHA/准确版本与不可变引用 |
| [images.py](../../packages/persistence/images.py) | 图像 FOR SHARE current read；不依赖旧 RR 快照签发 |
| [s3.py](../../packages/storage/s3.py) | 固定显式凭据本地 presign_image_get；精确 URL 范围与 TTL 复核 |
| [rate_limit.py](../../packages/application/rate_limit.py) | download_grant，共用 120/min 用户配额，Redis 失败不降级，不占写配额 |

没有向 AI 环境引入 S3、数据库或业务授权依赖。Worker 凭据保持独立，下载仅用 API secret，其允许范围见 [IRR-04 操作矩阵](../06-security/01-security-privacy.md)。

## 3. 顺序与事务边界

1. HTTP 验 UUID/variant/query，应用 preflight 检查身份；download_grant 在数据库事务外用 Redis 检查共享用户配额。不是信任 preflight 的旧权限。
2. 开始短 RR 事务。authenticate 持有 tenant→user/roles→session 当前锁；重新检查会话、epoch、用户/tenant 状态和权限。按 tenant+image_id 对 image FOR SHARE 作当前读，直到提交都阻止图片状态/版本/对象引用被并发修改。下载不锁 owner 聚合，不修改领域状态；READ 范围与 getImage 一致。
3. 先核可见性/READ，再核 original 的 Admin，最后核 ready 和所选 variant 完整证据。key 必须精确匹配 `tenant/{tenant}/lab/{lab}/{variant}/{image_id}/{sha}.{ext}`；A 固定 PNG，O 对应 JPEG/PNG/WebP。SHA 小写 64 位，VersionId 非空/非 null、长度≤200、可打印 ASCII；引用不能来自客户端。
4. 持有上述锁时 SDK 使用固定显式凭据作本地 SigV4，GET 参数为 Bucket、规范 Key、准确 VersionId，ExpiresIn=60。此处没有 HEAD/GET/PUT、网络调用、provider credential chain 或签名后改域名。签名 URL 再核实际 HTTPS origin/path、唯一 query、算法、credential scope、日期、60 秒、host signed header、版本、signature 格式；异常返回脱敏依赖错误。
5. 应用复核有效期限；original 同事务 append image.download_original 审计：actor/tenant/image_id、variant、固定访问原因、request_id。无原图字节、URL、签名、token、对象 key/version 或 SHA 进入审计。analysis 不记录原图敏感读取审计。
6. 成功提交后才将完整 grant 交给 HTTP，Cache-Control=no-store、Referrer-Policy=no-referrer。审计/提交失败则无 grant 到达调用者。图片、上传、任务、attempt、事件和幂等记录不变化；session idle touch 属于身份服务既有行为。

本地签名不保证对象存在或 IAM/反代可用。已登记版本被移除时 S3 GET 可能失败；API 不 HEAD，也不把图像改 rejected 或换成 latest。旧版本被新版本覆盖仍读取已登记准确版本，不能由对象 store 最新状态替代 DB 引用。

## 4. 签名能力与撤权

签名是 bearer 能力，不是实时 session 鉴权。撤权/禁用/删除提交后不再签发，既有 URL 最长仍可能在 60 秒内使用；不得声称撤 session 即撤所有已签 URL。签发时用户/图片锁可使并发撤权或删除等待短事务，但随后不能绕过新状态再次签发。

只把 URL 临时交给浏览器，原样使用并在页面设置 no-referrer；不写普通日志/监控 query/分析 SDK，不长期缓存。API 的 no-referrer header 不能替代前端页面和存储代理的部署策略。访问已完成/取消巡检的 ready 图像仍按 READ，与只读历史一致，不需要采集权限；原图仍额外 Admin。

本批不签发 staging/crop/reports GET，也不授予 list/delete 或新 IAM 通配范围；没有实现对象删除/紧急凭据撤销操作。

## 5. 本地验证

业务测试见 [test_image_download.py](../../tests/business/test_image_download.py)，真实 MySQL/HTTP/Redis 测试见 [test_image_download_mysql.py](../../tests/persistence/test_image_download_mysql.py)。新增 40 项业务用例、26 项真实 MySQL 用例。固定源图片、O/A 版本与结果为合成数据；本地 presign 使用真实 SDK/密码学，但不连接真实 MinIO。

| 验证 | 2026-10-02 实际结果 |
|---|---|
| 下载业务定向 | 40 passed；含独立 HMAC 复算 SigV4、方法/版本篡改核签反例 |
| 下载真实 MySQL 定向 | 26 passed，173.48s，无跳过；MySQL 8.0.33、43 表/115 FK、head=0001_initial |
| 业务/领域/安全/协议/持久化单元 | 1469 passed = 1444 主套件 + 4 协议 + 21 持久化单元，38.97s；1 项既有 Starlette/AnyIO 弃用警告 |
| 完整持久化 | 383 passed = 362 项真实 MySQL + 21 项单元，1212.38s，无跳过；1 项既有弃用警告；21 项单元与主套件重叠 |
| 最终响应与身份边界定向 | 49 passed，9.09s；包含 no-store/no-referrer 响应与既有 /ready 范围兼容 |
| 静态/格式/依赖 | Ruff check PASS；132 files format PASS；pip check PASS |
| 契约/设计/文档 | build_specs --check 16 制品未漂移；148 synthetic readiness；validate_specs PASS、277 文档链接 |

主要风险覆盖：schema/默认 variant/strict query；四类实验室读者 analysis 可读而 original 拒绝；tenant/lab 404；非 ready/坏 key/hash/version 不调用 SDK；真实 URL 编码特殊 VersionId；签名绑定 GET/Host/path/version/60 秒并拒绝异常 URL；preflight 后撤会话/改角色/删图拒绝；签名期间另连接 FOR UPDATE NOWAIT 证明 user/image 锁有效；审计失败无返回 URL、无审计残留；每次原图读取独立审计；Redis 失败/429 不签发；限流在事务外。

### 可复现命令

~~~powershell
.\.venv-business\Scripts\python.exe -m pytest tests/business tests/domain tests/security tests/protocol tests/persistence/test_unit.py -q
$env:LABSAFE_TEST_REDIS_SERVER = '<专用 Redis 可执行文件路径>'
.\.venv-business\Scripts\python.exe tools/database/run_mysql_tests.py --mysqld '<专用 MySQL 8.x mysqld 路径>'
.\.venv-business\Scripts\python.exe -m ruff check apps packages tests tools/database
.\.venv-business\Scripts\python.exe -m ruff format --check apps packages tests tools/database
.\.venv-business\Scripts\python.exe -m pip check
python -B tools/design/build_specs.py --check
python -B tools/design/test_readiness_design.py
python -B tools/design/validate_specs.py
~~~

定向可临时 PYTEST_ADDOPTS='-x -k image_download'；完整回归移除过滤变量。MySQL/Redis 启动器只创建/清理自己的临时实例。完整持久化套件含 21 项与主套件重叠的单元，不累计成互不重叠的测试数量。AI/前端本批未修改；远程 CI 与独立审批未执行。

本地代码与验证批次已关闭；真实存储部署验收和完整系统阶段保持未完成。前序批次的验收数字保留为历史证据，不替换成 F4 的累计结果。

## 6. 回退与未关闭项

恢复 API_UPLOADS_ENABLED=0 并重启 API 可停止签发，同时关闭上传入口；不删除任何 DB/对象/审计。已签 URL 自然到期，不承诺即时失效；紧急事件按安全处置流程处理存储凭据。后台已登记任务不受签名入口停止影响。

真实部署 UP-03/SEC-03 仍需完成：versioned MinIO 同源 HTTPS 实际 GET 原/分析准确版本并比对 SHA；重复 PUT 不换已登记版本；篡改 method/Host/path/version/签名、过期 URL 的实际拒绝；跨 tenant/lab/staging/crop 的 IAM 负例；代理保持签名参数、访问日志不记录完整 query、页面 no-referrer、缓存策略与凭据撤销实测。SDK/HMAC/MySQL 成绩均不能代替以上验收。完整 I-02/I-03、crop/报告下载、业务提交与独立 AI 执行、生产发布仍待完成。
