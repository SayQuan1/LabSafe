# 03.1 API、消息与命令语义

## 1. 契约入口

| 契约 | 用途 |
|---|---|
| [public-api-v1.yaml](../../contracts/public-api-v1.yaml) | /api/v1 公共字段、枚举、请求、响应、路径参数、权限动作 |
| [inference-v1.yaml](../../contracts/inference-v1.yaml) | /internal/inference/v1 独立AI协议 |
| [operation-catalog.json](../../contracts/operation-catalog.json) | 完整 operationId 与方法/路径索引 |
| [error-codes.json](../../contracts/error-codes.json) | 错误 HTTP码、是否可重试 |
| [task-message-v1.json](../../contracts/task-message-v1.json) / [events-v1.json](../../contracts/events-v1.json) | 判别联合消息，不允许任意 payload |

生成版本为1.1.0；本期未部署，旧示例不兼容时直接按此契约重建客户端，不承诺旧消费者自动兼容。不得发送未定义字段；不能使用 data:{}、无类型 items 或任意对象作为业务契约。

## 2. HTTP、分页、鉴权与错误

- 成功统一 {data,request_id}；失败 {request_id,error:{code,message,retryable,details}}。message 只展示，不用于业务分支。details 只含字段和安全原因，不含密码、token、内部SQL。
- 创建资源201、异步受理202、同步命令/查询200；没有隐式204。GET 无副作用。列表 page默认1，page_size默认20、最大100；默认 created_at DESC,id DESC。总数与页数据使用同一一致性快照；不是跨页冻结视图，导出另行冻结。
- laboratory_id 只允许影响具有实验室归属的数据。列表无此归属（词典、规则、模板、院系、用户）出现该参数应422，不静默忽略。列表总是先授权后计数；越权laboratory_id返回404。
- 会话cookie，写请求（除login）必须 Idempotency-Key 和 X-CSRF-Token。无身份401；存在但动作不足403；跨租户/范围或不可见ID返回404。
- 422 结构/业务输入非法；409 状态、并发、幂等冲突；413 大小；415 MIME；429 限流；503 暂不可用。异步任务业务失败不让后续GET变500，GET返回Job/Run的failed及error_code。
- /ready模型未就绪503；质量不足是200 needs_retake，不作为传输失败重试。

## 3. 幂等和并发

幂等作用域 (tenant,actor,method,规范路径,key_hash)，保留24小时。request_hash基于规范JSON；同key同body返回原状态/响应（request_id仍使用原响应），无任何重复审计/事件；同key不同body409 IDEMPOTENCY_CONFLICT。授权在重放响应前仍检查，已撤权不能获得缓存数据。

I-02B 身份写命令将上述规范 JSON 的 UTF-8 字节以服务端秘密做 HMAC-SHA256 后存入 request_hash，避免 createUser 的初始密码被无密钥离线枚举；不是将明文密码或无密钥密码摘要写入幂等表。Idempotency-Key 是 8–128 个可见 ASCII 字符的 opaque 值，不进行文本规范化。重放采用统一键序 JSON，保留原 HTTP 状态、响应与 request_id；logout 成功已撤销会话，随后持该旧会话重放仍为 401，不绕过授权返回缓存 200。

API事务用行锁保证同key串行；pending lease=30秒，用于请求进程中断恢复。由于业务和响应同事务提交，崩溃无业务提交时可重新执行，有提交则有完整响应。外部上传/导出仅创建意图，不在事务内执行。另一个请求仍在执行返回409 REQUEST_IN_PROGRESS+Retry-After:2，客户端可用原key重试。

每次用户新意图生成新key；网络重发保留原key。expected_version 指向 [状态机](../02-architecture/02-domain-model.md)定义聚合；冲突需用户刷新并重新决定，不能后台自动把新版本填进去重发。认证login无幂等保证，以安全限流防滥用。

## 4. 查询与资源投影

- getCurrentInference/getCurrentFacts：未建立时404，不返回伪空资源；历史run按ID可读，UI标明不是当前。
- getJob：根据 resource_id 推导业务归属；不是知道task UUID就可以查看。deadletters仅admin。
- searchChemicals：q 必填时做NFKC+casefold后的名称/别名/CAS前缀搜索；空q分页返回指定词典内容；必须 dictionary_version_id，不允许混版本。结果最多100。
- listFindings 默认 current_only=true，过滤 superseded_at；历史查询可显式 current_only=false（仍授权）。统计只计当前；dashboard包括open confirmed/dispatched、high/critical、逾期tasks、待复核items。
- listEvidence 按created_at ASC,id ASC；列表字段只含本 task 的已提交证据。
- 图片download默认analysis；original额外授权并审计。crop通过单独证据下载路径，必须匹配 run/crop/image 闭包，不能传 object_key。
- notifications只取recipient=当前用户；read重复同key返回原结果，read_at首次设置后不再变化。
- listUserRoles供admin查看实际scope；listDictionaryEntries返回固定词典条目及aliases，未发布仅admin；getModelManifest仅admin返回制品键；RuleVersion.rules返回完整规则正文而不是只有ID，供独立专家审查。
- 所有 allowed_actions 来源于后端同源guards；不能作为客户端离线授权凭据。
- Session.environment取服务端APP_ENV；InferenceRun.is_simulated取该run固定manifest是否fixture-v1；/version同时回显purpose/is_simulated。worker/activation握手时核对，不能仅凭前端开发标识决定允许模拟。

## 5. 非状态机命令

| operationId / 类型 | 具体行为 |
|---|---|
| login/getMe/logout | 见安全规范；getMe返回有效csrf与permissions；logout撤销当前session并清cookie |
| createCollege/createLaboratory/createLocation | code同租户唯一；lab的college必须active；location必须同lab且无循环父链；room无parent、area父room、shelf/cabinet父room或area |
| createUser/grantRole/revokeRole/disableUser | admin；初始密码必须达策略；角色scope合法；更新epoch使所有旧session权限失效 |
| createTemplate | item.code唯一、sort_order唯一，1–100项，模板内ID必须互异；未发布仅admin可见 |
| createUpload | 验owner可采集、权限、大小/MIME、capture时间；生成10分钟PUT grant，按用户最多10个未完成grant |
| completeUpload | 固定对象版本；相同upload只生成一个image和一个验证job；客户端hash与grant/hash一致；之后通过getImage轮询 |
| deleteImage | 仅未被run/evidence/dataset引用且非legal_hold；原子标记/清理任务，不同步物理删除 |
| importDictionary | 条目ID/版本label唯一，CAS格式校验不等于科学正确；alias歧义保留并在匹配时降候选，不强行唯一 |
| importRuleVersion | rules Schema及语义验证；rule_set必须已存在；审批/发布不允许绕过测试case验证 |
| importModelVersion | 只登记受控制品且路径白名单；校验purpose/adapter/环境；production的validate/publish需真实报告及服务端批准台账，不信任客户端evaluation_passed=true；dev/test可登记有真实fixture文件hash的development bundle |
| createRuleBundle | 所有成员published且同tenant，无冲突重复rule；排序后hash；相同hash返回已存在snapshot |
| 容量前置守卫 | createInspection在写入前校验模板项×位置≤100；createRuleBundle校验合并规则≤200；异步推理/评估容量错误遵循事实与规则5.1，禁止提交后才发现响应/事件数组超限 |
| createExport | export权限；范围≤31天、≤100实验室、≤10000条finding；事务中冻结授权后的字段和关联ID快照；超量422要求缩小范围；创建job202 |
| downloadExport | 状态ready、24h内、创建人或同范围admin且仍有导出权限；短期签名60秒；不再查当前业务数据重生成 |
| readNotification | 校验recipient，首次设置read_at、version+1；其他人404 |
| replayJob | 只允许当前资源且终止任务，重新检查制品/owner；见持久任务，非“覆盖旧结果” |

### 5.1 角色管理补充（I-02C）

- grantRole/revokeRole 的 expected_version 指向目标 User.version；laboratory_id 为必填 nullable 字段。tenant 角色只允许 safety_admin/rule_expert 且 laboratory_id=null，其余四角色必须 laboratory scope 并引用同租户实验室。
- 授予仅面向 active 用户和 active 实验室；撤销可清理 disabled 用户/archived 实验室的已有精确 assignment。新 key 重复授权返回 STATE_CONFLICT，精确 assignment 不存在的撤销返回 NOT_FOUND；版本不匹配优先返回 VERSION_CONFLICT。
- 每次成功变更只递增 User.version/session_epoch 一次并撤销目标全部会话。请求 schema 无 reason 字段，审计使用固定操作原因，不擅自加入请求字段。
- listUserRoles 的 laboratory_id 仅筛选匹配的实验室 assignment，跨租户在计数前为 404。授予不得使去重后的 Session.permissions 超出既有 200 项容量，超出原子回滚，不能截断。详细接入与证据见 [I-02C 验收](../08-delivery/12-i02c-role-api.md)。

### 5.2 组织、模板和巡检草稿补充（I-02D）

- College 无 published 状态；同租户 active 学院的既有白名单响应是任一有效角色可读的组织选择器公共投影，archived 仅 admin 可见。该解释不扩展到实验室业务或其他配置。模板普通角色仍仅可读 published，admin 可读全部状态。
- TemplateItem.id 按创建 Schema 由请求提供；其 id/code/sort_order 在模板内分别唯一。模板顶层 ID 由服务端生成，新 family 的 family_id=该 ID、revision=1。clone 为同 family 的 max revision+1、新 draft/version=1、全新子项 ID，不更新源模板；expected_version 检查源模板 version，不是 revision。publish/clone 保留操作者 reason。
- createInspection 原子建立全部模板项×位置草稿子项，含 required=false 项；容量与位置唯一性在写入前检查。重放以当前权限/结果可见性为准，不再次执行原状态前置条件。
- 本地接入、并发修复和测试见 [I-02D 验收](../08-delivery/13-i02d-foundation-api.md)；该批不含巡检项查询。后续查询/上传授权/完成受理分别见 5.4/5.5/5.6；图像验证消费、巡检完成/取消和任务闭环仍未实现。

### 5.3 导出快照

CSV列固定：inspection_id,item_id,laboratory_id,location_id,run_id,fact_revision_id,rule_bundle_id,finding_id,rule_id,severity,status,explanation,task_status,review_outcome,snapshot_at；每finding一行，零finding item一行且finding列空；日期UTC。用户文本以=,+,-,@或tab/CR开头时前置单引号，防公式注入。PDF同快照，按lab→inspection→item分组，附证据缩略图、版本、人工意见、无法判断提示；不能展示原图GPS或无授权身份数据。

### 5.4 巡检项查询与阶段能力（I-02E）

- listInspectionItems 接受 page/page_size/laboratory_id，先授权父巡检再计数；getInspectionItem 不接受 query。父巡检不存在/不可见为 404；可见且无子项为 200 空页。筛选 lab 不存在/不可见为 404，同时可见但不匹配父巡检 lab 为 200 空页。
- I-02E 接通这两个 GET 时累计 28 个公共业务 API；I-02F1/F2 新增上传授权/完成/图像查询后为 31 个。尚无 submit/retry/complete 巡检项写命令，因此 allowed_actions 固定为空能力集的投影，不能按状态补出前端按钮。
- 领域动作探测直接复用 submit_item/retry_item/complete_item 守卫；未加载上下文与已核验空集合不同。启用动作必须同时具备真实命令事务、服务器完整可信上下文及同源回归；不是仅修改 capability。
- 精确字段白名单、快照/权限和后续关闭条件见 [I-02E 验收](../08-delivery/14-i02e-item-query-api.md)。不修改公共 Schema、operationId 或生成源。

### 5.5 上传授权与阶段能力（I-02F1）

- I-02F1 接通 POST /api/v1/uploads（createUpload，201）；API_UPLOADS_ENABLED 默认 0，依赖真实身份配置。未配置为 503，仅 dev/test。I-02F2 的 completeUpload/getImage 见 5.6；图片下载及解码/Worker 尚未接通，不能在获得 grant 或完成 PUT 后展示 image ready。
- 请求严格为 owner_type、owner_id、filename、mime_type、size_bytes、sha256、captured_at，无 tenant_id/object_key/query；大小 1–15 MiB，JPEG/PNG/WebP，SHA 为 64 位小写十六进制。captured_at 为带时区 RFC3339，落库为 UTC 毫秒；不擅自添加新鲜度/未来窗口，不将客户端时间当可信证据。
- Item：当前 capture 权限及原创建者/admin，父巡检 draft/in_progress，子项 draft/uploaded/needs_retake/needs_review/failed。Task：当前受派人及 assignee 权限，in_progress/rejected；admin 不自动代替受派人，父巡检完成不阻断合法整改证据。
- 独立 10 次/分钟上传授权限流叠加普通写限流；每用户同时至多 10 个尚未过期的 granted，使用数据库锁保护。过期 grant 即使尚未被 sweeper 改状态也不占额度；Redis 不可用时拒绝而非绕过限流。
- grant 的 staging key 由服务端生成；PUT URL 同源 HTTPS:443、有效期 600 秒并签入 Content-Type/Host。相同 key/正文重放返回原响应/原到期时间，仍重验当前权限；过期 URL 不通过重放续签，新意图用新 key。
- HEAD 已在 I-02F2 接到完成受理；固定版本 GET/SHA helper 仍未接到 Worker。真实桶版本化、IAM、反代大小限制和 MinIO 签名拒绝需另行实测，不能用 SDK/Stubber 成绩替代。授权实现、锁序、配置与历史证据见 [I-02F1 验收](../08-delivery/15-i02f1-upload-grant-api.md)。

### 5.6 上传完成受理和图像元数据（I-02F2）

- POST /api/v1/uploads/complete 只接收 `{upload_id,sha256}`，202 返回 Image；GET /api/v1/images/{id} 返回同一 Image 白名单元数据。不接 query/客户端对象引用，共用上传开关、no-store 和既有鉴权错误边界。
- 首次完成先重验采集权限/owner、grant 未过期和声明 hash，退出并回滚准备事务后才 HEAD；取得准确版本/大小/MIME 后，提交事务再次认证并锁 owner→upload→image→task。期间撤会话、owner 变为不可采集、grant 过期或正文元数据变化均拒绝，不以预检结果提交。
- 同事务写入 image=validating、upload=validating、validate_image task_runs=ready、TaskDispatch Outbox=pending、审计和幂等响应。无上传完成领域事件新枚举，不提前发 ImageValidated，不直接发 Redis。original_sha256 在 validating 阶段仍是待验证声明值。
- 同 key/正文重放返回原 202/原响应/request_id；新 key 命中已完成 upload 返回同一 image 当前投影，不新建/不再 HEAD。两者都重验当前 capture/assignee 权限和声明 hash，不因原 grant 过期或 owner 状态已推进而重复创建；已删除 image 的完成请求 404，不复活资源。
- getImage 只要求 owner.read，读权限不等于采集/受派权限；已删除图像可返回契约中的 deleted 元数据墓碑，但这不授予任何下载能力。内部 key/version、upload_id、tenant_id、legal_hold 和 task payload 均不公开。
- 本批没有发布/领取/消费/图像 ready 提交；真实字节 SHA、解码、O/A 生成和固定版本缺失处理仍属于后续 Worker。实现、故障证据和依赖关闭清单见 [I-02F2 验收](../08-delivery/16-i02f2-upload-completion-api.md)。

## 6. 命令实现映射和变更

所有领域写请求遵循状态机和统一事务模板；返回值从本事务写后的资源投影生成。字段Schema不能替代guard。当前设计不支持通用删除组织/改密码等未列出的端点；相关需求只能走后续契约评审，禁止开发人员自行扩API。
