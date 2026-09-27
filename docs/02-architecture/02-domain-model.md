# 02.2 领域模型与命令状态机

状态：设计候选 1.1.0。命令请求和响应见 [公共协议](../../contracts/public-api-v1.yaml)；权限见 [安全规范](../06-security/01-security-privacy.md)。未列出的状态转移一律 409 STATE_CONFLICT，状态不可通用 PATCH。

## 1. 聚合和版本

Inspection → InspectionItem → InferenceRun → FactRevision → RuleEvaluation → Finding；Finding 最多一个 RemediationTask，Task 有多个追加 Evidence。每次事实/推理结果均引用实际模型、词典、规则 bundle，不随 activation 改写。

expected_version 指向 URL 中聚合根：item/finding/task/user/template/rule_version/model_version/activation/notification/job。派发请求锁 finding，并核 finding.version；不是新 task 的版本。创建 activation 时 expected_version=1；未存在时按初始化版本 1 返回，已存在时必须匹配并递增。未创建的资源其余创建请求不接受 version。

## 2. 巡检和采集

| 命令/事件 | 前置状态与守卫 | 原子写集与结果 |
|---|---|---|
| createInspection | 已发布模板，实验室/位置有效且同实验室；location_ids 唯一；模板项数×位置数≤100，否则422 VALIDATION_ERROR且无写入 | 创建 draft；按 template_items × location_ids 创建 draft items；模板 revision 不复制更新 |
| completeUpload | grant 未过期、对象存在、大小/hash匹配、owner 可采集 | 创建 validating image 和 validate_image job；上传完成不是已通过质量 |
| ImageValidated | owner 未取消/终结、版本/哈希匹配 | image=ready；draft item→uploaded；其他允许采集态保持；Inspection draft→in_progress |
| submitInspectionItem | item uploaded/needs_retake/needs_review；全部图 ready、同 owner/location，清单唯一；无 confirmed/dispatched/closed finding | submission_revision+1；旧 active run 标 superseded，旧 findings 标 superseded_at；新 run queued、task ready；冻结 activation 和图片、reference_date；item queued、清空 current fact/evaluation/review |
| retryInspectionItem | item failed 且无已确认 findings；旧 run 技术终态；原图仍可用 | 用旧图片和旧 pinned versions 建新 run，submission_revision+1，replay_of=旧 run；旧结果不变；配置缺失 409，不自动换模型 |
| worker 领取 inference_pipeline | item queued且仍为当前run；task ready/run queued或task retry_wait/run retrying；available_at已到 | 同事务领取lease；item quality_checking，run processing/stage quality；每次attempt从quality重跑 |
| quality 不通过 | lease和版本仍有效 | run needs_retake/stage done；item needs_retake；没有事实或候选 |
| quality 通过 | lease和版本仍有效 | item processing；run stage facts |
| facts/rules 成功 | 证据有效、版本一致、无 stale token | 原子提交事实、规则评估、候选；run completed 或 needs_review；item needs_review；允许零候选 |
| attempt 可重试失败 / lease回收 | 当前run未被替换；仍拥有有效token，或sweeper在同锁序下回收已过期token | 同事务task retry_wait、run retrying/stage queued、item queued；按available_at重试，不保留半成品事实 |
| job 终止/耗尽 | 当前 run | run failed/stage done，item failed；保留错误原因，禁止空结果成功 |
| completeInspection | draft/in_progress；所有 items completed，包括可选模板项（首期不提供跳过命令） | inspection completed；有 cannot_determine 的项仍可完成行政流程，但报告保留无法判断 |
| cancelInspection | draft/in_progress；没有已确认或已派发风险 | inspection cancelled；待处理 run superseded、任务 fencing 失效；保留历史和上传图片 |

Image 验证失败 image=rejected，item 若无其他 ready 图回 draft，否则保持 uploaded；不能把 MIME/hash失败当作 AI needs_retake。采集 owner：item 仅 draft/uploaded/needs_retake/needs_review/failed；整改 task 仅 in_progress/rejected。queued/processing/completed 禁止追加采集。重拍使用 submit 新图片；技术重试使用 retry 原图片，语义不能混用。

## 3. 人工事实修订与完成

facts 命令只能作用于当前 item needs_review；不得存在 current findings 的 confirmed/dispatched/closed。请求是 entities/relations/dates 的完整替换快照，不是增量 patch。只允许修改当前 run 已有 detection 的事实；不得添加虚构 detection、跨 run evidence 或词典外实体；source 必须 human。至少提供 reason。数组键分别为 detection_id、(source,target)、(detection_id,kind)，不得重复。

事务：锁 item→run→当前 findings；追加 revision=n+1；旧 findings 全部 superseded_at；创建 queued rule_evaluation 和 task；current_fact_revision 指向新修订、current_evaluation 指向排队评估、item processing。旧评估回调必须匹配 current_fact_revision，不能复活旧 findings。失败 item failed，但保留新事实；死信重放 rule job 可恢复此修订，不重新做视觉推理。

评估完成后生成全新 needs_review findings，item needs_review，不继承旧人工结论。初始 fact revision 只由 Worker 生成；revision 间保持原 run。规则 snapshot/reference_date 与该 run 相同，不自动应用最新规则。

completeInspectionItem 只能 needs_review 且当前评估 completed、没有未决 needs_review finding：

| outcome | 额外守卫 | 效果 |
|---|---|---|
| no_issue | U=false且C=false；所有候选rejected或零候选 | completed，记录人工、时间、reason |
| issues_confirmed | U=false且C=true | completed，风险和整改独立继续 |
| cannot_determine | U=true；即使C=true也只能选择本项 | completed，保留已确认风险和剩余不确定性，报告均显著展示 |

IRR-02：C表示当前finding存在confirmed/dispatched/closed；U表示当前finding存在cannot_determine，或当前evaluation含unknown真值/insufficient_facts原因。U由当前事实重新评估产生，不沿用初始AI outcome，也不能由客户端提交覆盖。insufficient_facts逐项检查：无bottle、无有效规则、bottle缺唯一resolved实体、未归属bottle的label；有效日期规则所需expiry缺失/冲突/不可解析；有效成对规则所需same_location未知，或same_location=true而adjacent未知。只检查当前适用规则所需日期/关系，不将无关缺失字段当确定风险。人工修订可消除可修订事实的不确定性；未能消除则cannot_determine。零finding本身不能推出U=false。上述三种outcome互斥；needs_review finding仍先阻止全部完成命令，非法outcome返回409 STATE_CONFLICT。

一旦 item completed，不直接编辑事实、补拍或重新打开；重新巡检建立新 Inspection。已派发任务仍须执行到销项或人工 cannot_remediate。

## 4. Finding 与整改

Finding 原子创建为 needs_review，不存在中间 ai_detected 暴露态。已 superseded finding 不接受任何业务命令。

| 命令 | 状态和守卫 | 转移/写集 |
|---|---|---|
| confirmFinding | needs_review，证据可用，review 权限 | confirmed，confirmed_by；审计和事件 |
| rejectFinding | needs_review，reason 必填 | rejected；保留规则及证据 |
| cannotdetermineFinding | needs_review，reason_code+reason 必填 | cannot_determine；不是已确认风险 |
| dispatchRemediation | confirmed，assignee 在实验室有 remediator，due_at>now | finding dispatched；新 task pending_dispatch；唯一 finding_id；通知 |
| acceptRemediation | pending_dispatch，当前 assignee | in_progress |
| submitevidenceRemediation | in_progress/rejected，当前 assignee；1–10 张 ready 且属于此 task 的图 | 追加 Evidence、latest_evidence；task pending_recheck |
| recheckRemediation | pending_recheck；evidence_id=latest；复查者不是 assignee/证据提交者 | task closed，closed_at；finding closed；同事务审计/通知 |
| rejectRemediation | 同上，reason 明确 | task rejected；finding 仍 dispatched |
| cannotremediateRemediation | 非 closed/cannot_remediate；admin、reason | task cannot_remediate；finding 保持 dispatched，不能伪装销项 |
| reassignRemediation | pending_dispatch/in_progress/rejected/cannot_remediate；有效 assignee/due | task pending_dispatch；更新受派人/期限，旧证据保留，不改变 finding |

pending_dispatch 的准确含义是“已派发、待接单”。is_overdue=(status不在closed/cannot_remediate且due_at<now)，是查询派生值，不是状态。cannot_remediate 单独计入未解决风险统计。

## 5. 配置发布命令

| 对象 | 命令链与守卫 |
|---|---|
| 用户 | create active；grant/revoke 锁 user.version 并变更 roles；disable 递增 session_epoch、撤销会话。不能禁用/撤销最后一名租户 safety_admin |
| 模板 | create draft；publish draft→published，至少一个 item且code唯一；clone 新 family revision，复制子项生成新ID；发布后不改正文 |
| 词典 | import 先校验条目/别名/来源→draft；publish 原子冻结正文、生成快照和 hash→published；已发布不改 |
| 规则 | import 创建某 rule_set 下一 revision draft；submit-approval→submitted；approve→approved，审批者不得等于 submitted_by；publish→published；retire 只影响新 bundle，历史仍可重放 |
| 模型 | import registered；validate校验manifest、实际制品和环境限制→validated；production另要求calibrated、真实报告和独立批准台账匹配，dev/test可使用显式development fixture；publish/activation再次检查相同环境门禁；retire不再允许新activation，历史制品保留 |
| rule bundle | create 只接收同租户 published rule_versions，按ID排序并计算 checksum；成员不可变 |
| activation | 模型/词典/bundle 已发布、manifest词典一致、pipeline/device支持；校验后原子替换。按用户命令审计；不改变旧 run |

配置归档/重命名、用户自助改密码、模型训练平台不属本期 API。紧急账号重置由受审计管理 CLI，见运维。
