# 06.1 身份、权限、对象与审计

## 1. 角色与动作

租户来自session，实验室范围来自user_roles；不得信任请求tenant_id。安全管理员tenant scope；lab_manager/inspector/remediator/viewer只允许laboratory scope；rule_expert允许tenant scope。role grant/revoke/disable需admin并保护最后一名管理员。

| x-permission | 允许角色 | 附加条件 |
|---|---|---|
| anonymous / authenticated | 无 / 有效session | anonymous只login |
| read | 任一有效角色 | 返回授权实验室数据；租户级配置仅可读published公共投影 |
| admin | safety_admin | 同tenant；敏感配置变更写reason |
| capture | inspector、safety_admin | 实验室范围；inspection创建者或admin可修改巡检 |
| review | inspector、safety_admin | 同lab；current证据、版本guard |
| dispatch | safety_admin、lab_manager | 同lab；受派人具有remediator |
| assignee | remediator | user_id=当前assignee，不因admin自动代替 |
| recheck | inspector、safety_admin | 同lab且非assignee/证据提交人 |
| capture_or_evidence | capture或assignee | 按owner_type分支，不可混用 |
| image_read | 有owner.read权限 | original仅safety_admin且必须审计；crop同证据授权 |
| rule_approve | rule_expert | 不等于submitted_by；不能由admin绕过 |
| export | safety_admin、lab_manager | 所有lab都在授权范围 |
| audit | safety_admin | 只本tenant |
| recipient | 任一已认证用户 | notification.recipient_id=当前用户 |

list/getUser为admin；getMe仅自己。模型/词典/规则draft仅admin（submitted规则允许rule_expert）；published普通用户仅安全投影，无制品key或审批个人敏感数据。无身份401、动作不足403、不可见对象404；列表、统计、导出、签名下载同样授权。

## 2. 密码、会话和CSRF

Argon2id：memory=65536 KiB、time=3、parallelism=1、随机16字节salt、32字节hash；登录可按新策略rehash。密码12–128字符、不记录/截断，bootstrap强制更换操作由CLI完成；拒绝项目提供的演示密码。登录错误统一“账号或密码错误”，不暴露账号存在。

会话token随机32字节base64url，数据库仅SHA256；Cookie labsafe_session Secure/HttpOnly/SameSite=Lax/Path=/，8小时绝对有效、30分钟idle、每请求更新idle不延长绝对期限。用户session_epoch不匹配立即401。登录生成新token，不复用旧session；logout撤销并清cookie；改权/disable撤销该用户全部session。

CSRF token为session token经独立CSRF密钥HMAC-SHA256派生，getMe/login返回，DB保存hash用于校验；所有写请求（login除外）验证header和同源Origin。login也检查Origin与JSON Content-Type，禁止跨站登录CSRF。CORS只允许部署域，禁止*+credentials。

## 3. 限流与输入

按账户+IP登录5次失败/15分钟后429，Retry-After给剩余秒数；普通API用户120请求/分钟、写60/分钟、上传grant10/分钟，初始固定窗口Redis计数，Redis故障时login与写请求fail-closed 503，GET按本地保守限流继续且记录告警。处理X-Forwarded-For仅信任受控反向代理。

公开JSON≤2MiB，词典import≤10MiB，图片≤15MiB且40M像素；按真实解码MIME验证，不接受SVG/动图。拒绝JSON未知字段、过长数组、路径穿越。分析图去GPS/EXIF；原图受限保存，不把EXIF当可信拍摄时间。

## 4. 对象和内部服务

所有bucket私有、对象versioning。Web只有单对象短期PUT/GET grant，无长期密钥；下载60秒、上传10分钟。AI只读analysis；Worker按下表覆盖staging读取、original/analysis生成、derivatives/reports写入和独立cleanup凭据。API签名仅针对已授权DB记录生成的key。禁止客户端提供任意key或URL让AI抓取。

### 4.1 IRR-04对象操作矩阵

所有范围仅包含该部署获准租户/实验室，不授权通配全bucket。S=staging/{tenant}/{upload_id}；O/A/D/R分别为tenant/{tenant}/lab/{lab}/original/、analysis/、derivatives/、reports/。具体key见持久化规范。下表Get包含HEAD；读取固定版本使用GetObjectVersion，非版本HEAD/Get使用GetObject。

| 身份/secret用途 | 允许操作及前缀 | 明确不授予 |
|---|---|---|
| API签名/HEAD身份 | S:PutObject、GetObject、GetObjectVersion；O/A/D/R:GetObject、GetObjectVersion | S的浏览器GET grant、业务对象写入、删除、ListBucket、管理权限 |
| worker-general常规身份 | S:GetObject/GetObjectVersion；O/A:GetObject/GetObjectVersion/PutObject；D:GetObject/GetObjectVersion（报告证据）；R:GetObject/GetObjectVersion/PutObject | DeleteObject*、bucket管理、模型目录写入 |
| worker-inference身份 | A:GetObject/GetObjectVersion；D:GetObject/GetObjectVersion/PutObject | S/O/R写入、任何删除、bucket管理 |
| ai-inference身份 | A:GetObject/GetObjectVersion | 任意写/删、original/staging/reports访问、ListBucket |
| cleanup身份（只供general的object_cleanup处理器） | O/A/D/R:GetObjectVersion/DeleteObjectVersion；按已登记清单精确key+version | 无versionId的DeleteObject、写入、ListBucket、bucket管理 |
| 部署/备份身份 | 管理bucket/versioning/lifecycle、按清单备份恢复；独立离线保管 | 不注入API/AI/常规Worker |

CopyObject不是独立IAM动作：source固定版本读取用GetObjectVersion，destination用PutObject；SDK操作名不能直接充当策略Action。初期不用multipart（≤15MiB），不额外授予相关动作。对象通配仅限规范前缀内的服务职责；有效IAM policy由实施生成并用真实MinIO正反例验证，不把本表当已部署策略。

cleanup凭据使用S3_CLEANUP_ACCESS_KEY_FILE/S3_CLEANUP_SECRET_KEY_FILE，仅general挂载。同一general进程能读取该secret，这是受信后台代码边界，不宣称进程内强隔离；应用只在cleanup处理器取用，仍须检查DB引用/legal_hold。孤儿扫描以受控服务写入清单为输入，不授予一般API目录列举能力；需全bucket盘点时由离线运维身份生成候选清单，再由cleanup逐项复核。S由部署身份配置24小时生命周期，不让cleanup扩权删除staging。

API服务身份拥有生成签名所需权限，但浏览器只得到经过owner/lab授权的单对象能力。原图GET额外admin+审计；不向浏览器签发staging GET、列表、删除或任意key请求。session撤权后禁止再签发，已签发GET最长仍可能在60秒有效期内使用；紧急泄露按事件处置撤销对应存储凭据/停止入口，不能声称已有签名即时随session失效。

### 4.2 IRR-05签名入口

浏览器签名端点为S3_PUBLIC_ENDPOINT=PUBLIC_ORIGIN，均是实际HTTPS origin，不带路径；服务端实际读写仍用内网S3_ENDPOINT。固定path-style、SigV4、S3_REGION=us-east-1；公开路径为/labsafe-private/{key}，代理保持Host、路径、查询参数和签名头。禁止签名后字符串替换minio:9000为公网域名。具体代理和测试见部署规范；不开放9000/console。

同源访问不配置跨域CORS；浏览器PUT原始Blob，Content-Type精确等于required_content_type，不用multipart/form-data。PUT的Content-Type及Host参与签名；SDK生成的X-Amz-*查询整体原样使用。15MiB由入口实际请求体限制和complete/解码复验共同执行，不能仅相信grant.max_bytes。GET包含准确versionId并按对应variant授权；签名是bearer能力，禁止日志记录完整query、复制到分析系统或Referer泄露。

AI HTTP只在内部网络开放、Bearer token常量时间比较；反向代理禁止暴露内部路径。生产跨主机链路需要TLS；单宿主隔离bridge流量不暴露公网。token通过Docker secrets文件注入，每90天轮换，过渡最多24小时双token；日志不记录任何token/签名URL。

## 5. 审计与隐私

所有写命令和敏感读取（原图、导出、审计查询）记录actor、tenant、resource、action、before/after白名单、reason、request_id、时间；业务变更与审计同事务，写审计失败业务回滚。密码/token/原图/OCR全文不进入普通日志。审计追加写、应用无DELETE权限；管理员不能在API修改审计。

报告展示无法判断，不宣称绝对安全。人脸/无关人员画面只允许经授权原图访问；分析数据集需授权、脱敏和可撤回来源记录。生产数据不随文档提交Git；模型/data许可核验未完成不得发布。

## 6. 事件处置

发现泄露先撤销session/token、停止签名访问、保全审计；发现误报/漏报先记录实际版本、暂停受影响activation或回滚到已批准版本。不得删除历史结果掩盖问题。24小时内形成事件记录，恢复需复跑冻结回归集并由安全管理员及规则专家共同确认。该系统不能作为唯一安全依据。
