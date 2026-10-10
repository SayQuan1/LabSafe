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
- I-02E 接通这两个 GET 时累计 28 个公共业务 API；I-02F1/F2 新增上传授权/完成/图像查询后为 31 个；I-02G1 新增 `submitInspectionItem` 后为 32 个。submit 已有真实事务入口，但查询端仍不推断默认图片选择，因此 `allowed_actions` 暂固定为空能力集的投影，不能按状态补出前端按钮。
- 领域动作探测直接复用 submit_item/retry_item/complete_item 守卫；submit 的真实命令已由 I-02G1 接通，retry/complete 仍未接通。未加载上下文与已核验空集合不同。启用动作必须同时具备真实命令事务、服务器完整可信上下文及同源回归；不是仅修改 capability。
- 精确字段白名单、快照/权限和后续关闭条件见 [I-02E 验收](../08-delivery/14-i02e-item-query-api.md)。不修改公共 Schema、operationId 或生成源。

### 5.5 上传授权与阶段能力（I-02F1）

- I-02F1 接通 POST /api/v1/uploads（createUpload，201）；API_UPLOADS_ENABLED 默认 0，依赖真实身份配置。未配置为 503，仅 dev/test。I-02F2 的 completeUpload/getImage 见 5.6；F3 的验证结果与 F4 下载见 5.7，不能在获得 grant 或完成 PUT 后展示 image ready。
- 请求严格为 owner_type、owner_id、filename、mime_type、size_bytes、sha256、captured_at，无 tenant_id/object_key/query；大小 1–15 MiB，JPEG/PNG/WebP，SHA 为 64 位小写十六进制。captured_at 为带时区 RFC3339，落库为 UTC 毫秒；不擅自添加新鲜度/未来窗口，不将客户端时间当可信证据。
- Item：当前 capture 权限及原创建者/admin，父巡检 draft/in_progress，子项 draft/uploaded/needs_retake/needs_review/failed。Task：当前受派人及 assignee 权限，in_progress/rejected；admin 不自动代替受派人，父巡检完成不阻断合法整改证据。
- 独立 10 次/分钟上传授权限流叠加普通写限流；每用户同时至多 10 个尚未过期的 granted，使用数据库锁保护。过期 grant 即使尚未被 sweeper 改状态也不占额度；Redis 不可用时拒绝而非绕过限流。
- grant 的 staging key 由服务端生成；PUT URL 同源 HTTPS:443、有效期 600 秒并签入 Content-Type/Host。相同 key/正文重放返回原响应/原到期时间，仍重验当前权限；过期 URL 不通过重放续签，新意图用新 key。
- HEAD 已在 I-02F2 接到完成受理；固定版本 GET/SHA 已由 F3 Worker 使用。真实桶版本化、IAM、反代大小限制和 MinIO 签名拒绝需另行实测，不能用 SDK/Stubber 成绩替代。授权实现、锁序、配置与历史证据见 [I-02F1 验收](../08-delivery/15-i02f1-upload-grant-api.md)。

### 5.6 上传完成受理和图像元数据（I-02F2）

- POST /api/v1/uploads/complete 只接收 `{upload_id,sha256}`，202 返回 Image；GET /api/v1/images/{id} 返回同一 Image 白名单元数据。不接 query/客户端对象引用，共用上传开关、no-store 和既有鉴权错误边界。
- 首次完成先重验采集权限/owner、grant 未过期和声明 hash，退出并回滚准备事务后才 HEAD；取得准确版本/大小/MIME 后，提交事务再次认证并锁 owner→upload→image→task。期间撤会话、owner 变为不可采集、grant 过期或正文元数据变化均拒绝，不以预检结果提交。
- 同事务写入 image=validating、upload=validating、validate_image task_runs=ready、TaskDispatch Outbox=pending、审计和幂等响应。无上传完成领域事件新枚举，不提前发 ImageValidated，不直接发 Redis。original_sha256 在 validating 阶段仍是待验证声明值。
- 同 key/正文重放返回原 202/原响应/request_id；新 key 命中已完成 upload 返回同一 image 当前投影，不新建/不再 HEAD。两者都重验当前 capture/assignee 权限和声明 hash，不因原 grant 过期或 owner 状态已推进而重复创建；已删除 image 的完成请求 404，不复活资源。
- getImage 只要求 owner.read，读权限不等于采集/受派权限；已删除图像可返回契约中的 deleted 元数据墓碑，但这不授予任何下载能力。内部 key/version、upload_id、tenant_id、legal_hold 和 task payload 均不公开。
- F2 自身只受理；A1/A2 已接持久调度/执行，F3 已本地接真实字节 SHA、解码、O/A 生成和 ready/rejected 事务。历史实现与故障证据见 [I-02F2 验收](../08-delivery/16-i02f2-upload-completion-api.md)，F3 现行边界见下节。

### 5.7 图像验证结果与受控下载（I-02F3/F4）

- F3 的 opt-in general Worker 只消费 validate_image；准确 GET、实算 SHA、真实 Pillow 解码、一次 EXIF 旋转、去元数据 RGB PNG 后登记 O/A 和 ready。内容拒绝登记 image/upload=rejected、task=succeeded；技术故障保留 validating、自动重试/技术终态。ImageValidated 只在成功时写 Outbox，下游发布/inbox 未接通。见 [F3 验收](../08-delivery/19-i02f3-image-validation.md)。
- F4 接通 GET /api/v1/images/{id}/download（downloadImage），默认 variant=analysis，original 额外 Admin+原图读取审计。两者均按图像所属实验室 READ，只有 ready 且所选 O/A key/SHA/版本完整可签发，非 ready 返回 409。只允许单个 variant 参数，拒绝客户端对象 key/version/tenant 或额外 query。I-02I3 接通 GET /api/v1/reports/exports/{id}/download：ready、24h 内的 CSV 才能按创建人或同范围 admin 签发，且完整快照范围重新具备 export 权限；签名固定准确 key+VersionId、60 秒，不重新生成。
- DownloadGrant 仅 `{url,expires_at}`；同源 HTTPS SigV4 GET、固定数据库 VersionId、60 秒。签名原样使用，不跳到最新对象。API 返回 no-store/no-referrer，不重定向；原图审计与签发事务一起提交，审计失败不返回 grant。GET 不要求写命令头；每次原图签发独立审计，不做幂等缓存。
- 当前身份/角色/session 和图片记录在事务内锁定，SDK 签名为本地操作，无 HEAD/GET 或凭据刷新。下载不改变 image/upload/task/event。签发不证明对象仍存在或真实 IAM/TLS 可用；对象 GET 失败不能据此更换 VersionId 或伪造成功。
- 共用 120 次/分钟用户限流，Redis 故障不签发；不占写配额，不使用普通元数据读的降级。撤权后禁止新签发，既有 URL 到期前最长 60 秒仍可能有效。沿用 API_UPLOADS_ENABLED 和 API S3 secret；无新增迁移/配置。见 [F4 验收](../08-delivery/21-i02f4-image-download.md)。crop 与真实部署验收仍未完成；报告下载见 [I-02I3 验收](../08-delivery/30-i02-report-download.md)。

### 5.8 提交巡检项与推理入队（I-02G1）

- `POST /api/v1/inspection-items/{id}/submit` 接收 `ItemSubmit`，返回 `InferenceRun` 和 202。客户端只传 `expected_version` 及 1–3 个有序图片选择；服务端加载 item、父巡检、完整当前 findings、图片 metadata 和 laboratory activation。
- 事务锁定 item 与完整图片/当前 findings，复用 `submit_item` guard。旧 run/evaluation/findings 只标记 superseded；新 run 固定已发布 model/dictionary/rule、pipeline、device、UTC reference date、图片 SHA/顺序与 input hash，并原子写入 `run_images`、`inference_pipeline` task、TaskDispatch outbox、审计和幂等响应。
- 请求不能携带 tenant、laboratory、model、rule、device、date、object key/version 或 finding 列表。跨租户/实验室、非 ready 图片、版本过期、确认/派发/关闭 finding、activation 未发布均拒绝；任何写步骤失败整事务回滚。
- 本端点只登记持久任务，不声明 AI 已执行或产生结果。独立推理 worker 的 lease、D-FINE-N/GPU 执行、超时、fencing、retry/replay 和结果提交属于后续 I-03/AI 批次；`allowed_actions` 在查询端仍不凭状态推断默认图片顺序。

## 6. 命令实现映射和变更

第36批同步内部InferenceRequest：ImageRef新增必填object_version，Worker仅从run_images冻结版本构造，SHA和版本进入请求hash；缺失/空/null/控制字符/超过200字符均拒绝。submitInspectionItem公共请求不新增版本字段，版本由服务端锁定asset冻结。此为未部署1.1.0候选的内部破坏性变更，旧消费者须同步升级，禁止兼容回退到latest；外部既有消费者需另行迁移。当前真实CPU受控输入仅产出开发报告，HTTP仍mock，未改变InferenceResult事实/证据闭包。

所有领域写请求遵循状态机和统一事务模板；返回值从本事务写后的资源投影生成。字段Schema不能替代guard。当前设计不支持通用删除组织/改密码等未列出的端点；相关需求只能走后续契约评审，禁止开发人员自行扩API。

### 5.9 独立OCR文字证据（第37批）

InferenceResult新增必填text_regions≤100：line_id/image_id/crop_id、raw_text、confidence、quad和nullable detection_id。CropRecipe新增必填line_id及evidence（源RGB、PNG SHA/大小、识别旋转/尺寸/像素SHA），其detection_id可NULL，≤100。文字与crop必须属于当前run/image、一对一且quad/检测关联一致，关联真实检测时须同图存在；Schema之外执行共享闭包与UUIDv5身份校验。OCRField仍用于已关联目标的语义字段，独立原文不自动归为化学/日期事实。

公共InferenceRun在原有读取授权下增加只读text_regions，旧结果无该字段返回[]。EvidenceRef的nullable detection_id/crop_id可引用当前run的独立文字crop，禁止将line_id伪造成主体检测ID；本批未实现crop下载接口。内部旧结果缺必填字段拒绝，Worker/fixture及后续真实AI协调升级，候选版本不代表已部署批准。见[第37批](../08-delivery/37-i-ml01-worker-ocr-evidence.md)。

### 5.10 实际CPU身份（第38批）

内部Version新增必填model_checksum/dictionary_sha256，InferenceResult增加可选execution_identity（完整Version）。真实CPU响应必带，Worker按受控expected-version和ready/version逐项核验，且身份与结果模型/词典/pipeline一致；旧fixture响应可由Worker补入已核模拟身份。Version未ready为503，不用请求回显代替实际加载身份。旧缺摘要Version拒绝，AI/Worker协调升级；公共InferenceRun既有is_simulated从已登记execution_identity投影，历史或未有结果默认true，不增加公共字段、不重写历史。development bundle是本地工程绑定，不等于完整ModelManifest/activation/现场批准；见[第38批](../08-delivery/38-i-ml01-resident-cpu-http.md)。

### 5.11 文字到瓶子关联（第39批）

内部/公共TextRegion及CropRecipe字段形状保持，nullable detection_id明确指同图真实COCO bottle（class 39）。按text-bottle-quad80-v1中心/实际quad交面积≥80%/唯一最小框选择；无候选/并列NULL。line/crop关联与quad一致；AI/Worker重算全部几何选择，不接受任意同图检测、错误几何或遗漏唯一关联。无伪label/shelf/cabinet，无化学/日期字段，runs仍needs_review。旧输出可能不符合新语义门禁，AI/Worker与development代码锁协调升级，历史投影只读保留，不补写旧关联；见[第39批](../08-delivery/39-i-ml01-text-bottle-association.md)。

### 5.12 OCR字段与全部行证据（第40批）

OCRField新增必填source_lines，1–2项OCRFieldSource，逐项包含line_id/crop_id/raw_text/confidence；image_id/detection_id指同图真实唯一bottle，crop_id仅表示首行，全部来源由source_lines引用。字段raw_text按单LF连接，confidence取min；规范文本由ocr-fields-v1从正式文字生成。AI/Worker按全量有序重算核遗漏、伪值、跨图/瓶和容量；坏字段整批拒绝。runs仍needs_review，无entities/DateFact或事实快照。公共InferenceRun本批不新增字段投影或下载端点；历史JSON不回填。未部署候选内部兼容变更要求AI/Worker/lock同步升级，见[第40批](../08-delivery/40-i-ml01-ocr-fields.md)。

### 5.13 冻结开发词典与名称候选（第41批）

DevelopmentDictionary.entries升级有界对象，0–100条entity_id/canonical_name/aliases/cas/source，语义核重复/空名称/坏CAS和实际≤64KiB字节容量。DevelopmentCPUBundle新增entity_min并采用development chemical-v2 profile，内部Version允许该profile，完整ModelManifest的正式发布profile保持独立。

InferenceResult增加extraction_context（bundle_json/dictionary_json各≤64KiB UTF-8）。chemical-v2响应必带，Worker按固定model_checksum/dictionary_sha256重算原始字节摘要及ID，使用bundle内阈值核全部字段/候选/共识；原始内部路径只作摘要验证数据，不读响应指定文件。实际CPU和所有候选响应与消费者需同步升级，无快照非空entities拒绝；公共InferenceRun仍既有授权/文字投影，不暴露内部快照/路径或新增下载端点。仍needs_review，无日期事实或自动安全判断；见[第41批](../08-delivery/41-i-ml01-chemical-candidates.md)。
