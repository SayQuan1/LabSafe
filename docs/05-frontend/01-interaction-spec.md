# 05.1 页面、状态和交互

## 1. 页面与后端映射

| 路由                     | 页面内容与数据                                  | 可执行操作                                                           |
| ---------------------- | ---------------------------------------- | --------------------------------------------------------------- |
| /login                 | 租户code、账号、密码；登录通用错误                      | login，成功加载getMe后跳/dashboard                                     |
| /dashboard             | 统计卡片、待复核/待整改入口；getDashboard              | 跳转已授权列表，无前端自行重算风险                                               |
| /inspections           | 实验室、模板、创建时间、状态；list/createInspection     | 选lab/template/location创建                                        |
| /inspections/:id       | items列表、各项状态、缺项提示                        | complete/cancel；按allowed_actions显示                              |
| /inspection-items/:id  | 图片清单、质量原因、处理阶段、原文/实体/关系、规则依据、人工记录        | submit/retry/facts/finding decisions/item complete              |
| /findings              | 当前findings，lab/status/severity筛选、历史开关    | 进入详情/派发；历史项只读                                                   |
| /remediation-tasks/:id | 时间线、受派人、期限、证据批次、复查意见                     | accept、submit-evidence、recheck/reject/reassign/cannot-remediate |
| /reports               | 时间范围/labs/format、生成状态、下载                 | createExport/getExport/downloadExport                           |
| /notifications         | 未读/已读消息，资源跳转                             | readNotification，跨权限资源404                                       |
| /admin                 | 院系、实验室、位置、用户角色、模板、词典、模型、规则、activation、死信 | 按对应契约表单/JSON导入，显示审批、版本与错误                                       |
| /audit                 | actor/action/resource/时间展示、分页            | 只读，不提供删除编辑                                                      |

admin JSON导入使用协议Schema校验并展示field错误，不允许表单提交 arbitrary JSON绕过后端。规则提交和审批分别展示用户身份，隐藏自己的批准按钮但后端仍须拒绝。模型发布显示真实评测引用，禁止用绿色图标暗示评测通过而无证据。

## 2. 状态和文案

根据Session.environment在dev/test全局固定显示“开发环境，结果不用于安全判断”；InferenceRun.is_simulated=true时在图像、结果和导出增加“模拟数据”水印/文字。后端environment是唯一依据，不能由浏览器参数关闭；生产返回is_simulated视为配置错误并禁止继续业务操作。模拟模型不增加客户端选择fixture的生产请求字段。

| 后端item状态                           | 页面                                   |
| ---------------------------------- | ------------------------------------ |
| draft/uploaded                     | 草稿/可提交；显示每张validating/ready/rejected |
| queued/quality_checking/processing | 排队/质量检查/处理中；显示run/job，禁重复提交和事实编辑     |
| needs_retake                       | 需补拍；逐图原因，重新选整个1–3张清单                 |
| needs_review                       | 待人工复核；零候选也展示“在本次图片和规则范围内未发现”并要求人工完成  |
| completed                          | 已完成人工复核，展示review_outcome，不显示“绝对安全”   |
| failed                             | 技术失败，显示稳定error_code、可恢复动作；不能作为未发现风险  |

run历史结果标“非当前结果”；过期GET响应不能覆盖新run页面。保留页内当前run_id+submission_revision，返回更旧版本丢弃并重新取current。

## 3. 上传与轮询

1. 本地预校验MIME/15MiB仅用于体验；展示拍摄隐私提示。
2. createUpload→PUT grant（失败只重传同授权对象，过期重新申请）→completeUpload→getImage；image ready之前不能提交。
   IRR-05：put_url必须是当前PUBLIC_ORIGIN的/labsafe-private/路径；原样使用query，Content-Type取required_content_type，Body为原始Blob，不能FormData或替换域名。前端不设置禁止手工设置的Host/Content-Length，不发送长期对象凭据。对象服务403/413分别提示重新申请grant/缩小文件；不得解析成业务JSON错误。GET grant也保持versionId与签名原样，原图仍额外授权。
3. 恰一overview，其余detail显式关联；移除未提交照片仅影响本地清单，需要删除服务器对象时单独确认deleteImage。
4. submit得到run202后轮询2秒，30秒后5秒，2分钟后10秒；标签隐藏暂停、恢复立即拉取。5分钟后停止自动轮询并显示“仍在处理，可刷新”，不推断失败。
5. 网络GET失败指数退避2/4/8秒后提示重连；不要自动重发用户的新业务意图。

所有写请求复用本次意图幂等键直到有终态响应；用户主动修改输入后新key。409版本冲突展示新旧变化并刷新；403收起操作；401清会话并跳登录；429遵循Retry-After。禁止以JS本地时间判断权限/截止或业务终态。

## 4. 复核与整改操作

同屏展示analysis图、归一化bbox、OCR原文和confidence、词典候选、rule_id/版本/依据；evidence点击请求受控crop下载。图层坐标随图片实际渲染宽高换算，不用屏幕像素回写事实。

facts编辑提交完整快照，必须reason；不新增检测框。保存后转processing并提示全部相关候选需重新复核。确认、驳回、无法判断分开按钮，不能默认选确认。无法判断必须reason_code；no_issue仅在guard满足时显示。

任务接单后才能提交整改证据；每批1–10图+描述，复查必须明确选latest_evidence_id。复查者不能是整改执行者；cannot_remediate不能显示“已销项”。重派弹窗要求新责任人、期限、原因。

## 5. 可用性要求

首期适配360px移动采集和1280px桌面复核；文件选择支持相机但不请求无必要地理位置权限。键盘可访问、表单label明确、错误关联字段，状态不能只靠颜色。确认派发、完成巡检、停用用户、删除图和版本激活需二次确认，展示影响范围。页面仅保留短期内存草稿，不把图/token/敏感OCR写localStorage。
