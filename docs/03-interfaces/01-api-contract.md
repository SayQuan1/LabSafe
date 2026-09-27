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

CSV列固定：inspection_id,item_id,laboratory_id,location_id,run_id,fact_revision_id,rule_bundle_id,finding_id,rule_id,severity,status,explanation,task_status,review_outcome,snapshot_at；每finding一行，零finding item一行且finding列空；日期UTC。用户文本以=,+,-,@或tab/CR开头时前置单引号，防公式注入。PDF同快照，按lab→inspection→item分组，附证据缩略图、版本、人工意见、无法判断提示；不能展示原图GPS或无授权身份数据。

## 6. 命令实现映射和变更

所有领域写请求遵循状态机和统一事务模板；返回值从本事务写后的资源投影生成。字段Schema不能替代guard。当前设计不支持通用删除组织/改密码等未列出的端点；相关需求只能走后续契约评审，禁止开发人员自行扩API。
