# Gate 2 详细设计

状态：Gate 2 已确认，Gate 3 暂缓

本文件将 Gate 0/1 架构转为可实现契约。它仍是设计文档，不包含业务代码、数据库迁移、依赖锁文件或部署脚本。

## 1. 设计基线

- 前端：Vue 3、TypeScript、Vite；组件库和状态管理仍需在本 Gate 确认。
- 后端：FastAPI、Python，业务主应用采用模块化单体。
- AI：独立 HTTP/JSON 服务，首期不接入 LLM。
- 数据库：MySQL 8.0+；业务字段结构化，模型证据少量使用 JSON。
- 对象存储：MinIO，使用 S3 兼容 API。
- 异步任务：数据库 Outbox、Celery、Redis。
- 通知：首期站内通知，外部渠道仅保留适配器接口。
- 租户：学校是 tenant，学院和实验室为下级组织；首期操作权限按实验室范围控制。
- 性能：推荐配置完整推理 P95 不超过 30 秒；CPU 模式允许异步超时，但必须可追踪、可重试、可人工兜底。
- 恢复目标：RPO 不超过 24 小时，RTO 不超过 4 小时，需在试点环境验证。

## 2. MySQL 数据库设计

### 2.1 通用约定

- 主键首选 CHAR(36) UUID；最终是否使用二进制 UUID 在迁移前确认。
- 时间使用 DATETIME(3)，统一保存 UTC。
- 业务表包含 tenant_id、created_at、updated_at；可删除实体增加 deleted_at。
- 不使用数据库行级安全策略；租户隔离由服务层强制注入 tenant_id，并通过复合索引和测试保证。
- 外键默认 RESTRICT；业务删除采用软删除或归档，审计事件不可物理删除。
- JSON 仅保存模型原始输出、规则输入事实和可变证据；权限、状态、租户和查询字段使用结构化列。

### 2.2 组织和权限表

| 表 | 关键字段和约束 |
|---|---|
| tenants | id、name、code UNIQUE、status、retention_policy_json |
| colleges | id、tenant_id、name；UNIQUE(tenant_id,name) |
| laboratories | id、tenant_id、college_id、name、code、status；UNIQUE(tenant_id,code) |
| locations | id、tenant_id、laboratory_id、parent_id、type、label、path |
| users | id、tenant_id、username、display_name、email、password_hash、status |
| roles | id、tenant_id 可空、code、name |
| user_roles | tenant_id、user_id、role_id、laboratory_id 可空；防重复唯一约束 |
| permission_scopes | tenant_id、user_id、resource_type、resource_id、actions_json |

tenant_id 必须来自当前会话，不允许客户端直接指定后写入。跨租户查询默认返回 404 或空结果，不泄露资源存在性。

### 2.3 巡检、图片和推理表

| 表 | 关键字段和约束 |
|---|---|
| inspection_templates | id、tenant_id、name、version、status、published_at |
| template_items | id、template_id、code、title、capture_hint、sort_order |
| inspections | id、tenant_id、laboratory_id、template_id、inspector_id、status、started_at、completed_at |
| inspection_items | id、tenant_id、inspection_id、template_item_id、location_id、status、submitted_at |
| asset_images | id、tenant_id、inspection_item_id、object_key、sha256、mime_type、width、height、captured_at、redaction_status、deleted_at |
| image_derivatives | id、tenant_id、image_id、kind、object_key、width、height |
| model_versions | id、tenant_id 可空、model_name、version、artifact_uri、config_json、status |
| inference_runs | id、tenant_id、inspection_item_id、idempotency_key、status、model_version_id、rule_version_id、quality_json、detections_json、ocr_json、entities_json、started_at、finished_at、error_code |

### 2.4 化学品、规则和问题表

| 表 | 关键字段和约束 |
|---|---|
| chemical_entities | id、tenant_id 可空、canonical_name、cas_number、hazard_class、storage_class、status |
| chemical_aliases | id、entity_id、text、language、source；UNIQUE(entity_id,text,language) |
| rule_sets | id、tenant_id 可空、name、scope、status |
| rule_versions | id、rule_set_id、version、dsl_json、checksum、status、approved_by、published_at |
| findings | id、tenant_id、inspection_item_id、inference_run_id、type、severity、status、confidence、uncertainty_reason、facts_json、evidence_json、confirmed_by、confirmed_at |
| finding_rules | finding_id、rule_version_id、rule_id、inputs_json、explanation_template、matched |

### 2.5 整改、通知和审计表

| 表 | 关键字段和约束 |
|---|---|
| remediation_tasks | id、tenant_id、finding_id、assignee_id、status、priority、due_at、description、closed_at；首期 finding_id UNIQUE |
| remediation_evidence | id、tenant_id、task_id、image_id、description、submitted_by |
| notifications | id、tenant_id、recipient_id、type、resource_type、resource_id、read_at、created_at |
| audit_events | id、tenant_id、actor_id、action、resource_type、resource_id、old_value_json、new_value_json、reason、request_id、created_at |
| outbox_events | id、tenant_id、event_type、aggregate_type、aggregate_id、payload_json、status、attempts、available_at、published_at |
| report_exports | id、tenant_id、requested_by、filter_json、format、status、object_key、expires_at |

### 2.6 索引、迁移和种子

- 租户查询索引以 tenant_id 为首列。
- 高频索引包括 findings(tenant_id,status,severity,created_at)、remediation_tasks(tenant_id,status,due_at)、inference_runs(tenant_id,status,created_at)。
- 迁移采用 expand、migrate、contract 顺序，避免试点期间破坏旧列。
- 每个迁移必须有向前迁移、回滚或补偿说明；发布前备份数据库。
- seed 只包含系统角色、权限、演示租户、演示模板和脱敏规则，不包含真实学校数据。

## 3. API 契约

### 3.1 通用约定

- 公共前缀为 /api/v1；内部推理服务使用 /internal/inference/v1。
- 每个请求解析 tenant_id 和实验室授权范围。
- 列表统一返回 items、page、page_size、total；默认 page_size 为 20，最大 100。
- 写操作支持 Idempotency-Key；重复请求返回第一次结果。
- 错误结构为 code、message、details、request_id。
- 排序字段使用白名单，禁止把客户端字段直接拼接 SQL。

### 3.2 认证和组织

POST /api/v1/auth/login
POST /api/v1/auth/logout
GET /api/v1/me
GET /api/v1/tenants/{tenant_id}/colleges
GET /api/v1/laboratories
POST /api/v1/laboratories
GET /api/v1/laboratories/{id}/locations
POST /api/v1/laboratories/{id}/locations
GET /api/v1/users
POST /api/v1/users/{id}/roles

### 3.3 巡检和推理

POST /api/v1/inspections
GET /api/v1/inspections/{id}
POST /api/v1/inspections/{id}/items/{item_id}/upload-url
POST /api/v1/inspections/{id}/items/{item_id}/images
POST /api/v1/inspection-items/{id}/submit
GET /api/v1/inspection-items/{id}/inference
POST /api/v1/inspection-items/{id}/retry

上传只接受服务端签发的短期对象存储凭证；API 校验对象存在、租户前缀和哈希。

### 3.4 Finding 和整改

GET /api/v1/findings
GET /api/v1/findings/{id}
POST /api/v1/findings/{id}/confirm
POST /api/v1/findings/{id}/edit
POST /api/v1/findings/{id}/reject
POST /api/v1/findings/{id}/cannot-determine
POST /api/v1/findings/{id}/remediation-tasks
GET /api/v1/remediation-tasks
POST /api/v1/remediation-tasks/{id}/accept
POST /api/v1/remediation-tasks/{id}/submit-evidence
POST /api/v1/remediation-tasks/{id}/recheck
POST /api/v1/remediation-tasks/{id}/reject
POST /api/v1/remediation-tasks/{id}/cannot-remediate

禁止通用 PATCH status；状态变化必须通过领域命令校验操作者、前置状态、证据和审计原因。

### 3.5 报表、规则和审计

POST /api/v1/reports/exports
GET /api/v1/reports/exports/{id}
GET /api/v1/rules
POST /api/v1/rule-versions/{id}/submit-approval
POST /api/v1/rule-versions/{id}/approve
POST /api/v1/rule-versions/{id}/publish
POST /api/v1/rule-versions/{id}/rollback
GET /api/v1/audit-events
GET /api/v1/notifications
POST /api/v1/notifications/{id}/read

### 3.6 HTTP/JSON 推理契约

POST /internal/inference/v1/runs
GET /internal/inference/v1/runs/{run_id}
GET /internal/inference/v1/health

请求包含 run_id、inspection_item_id、tenant_id、image_refs、model_version、rule_version 和 idempotency_key。响应包含 run_id、status、quality、detections、ocr_fields、normalized_entities、rule_candidates、模型/规则版本、错误码和 timing_ms。

推理服务必须实现幂等、超时、健康检查、稳定错误码和版本回显。gRPC 只作为未来协议适配，不改变业务语义。

## 4. 异步任务与事件

1. 业务事务写入领域表和 outbox_events。
2. Outbox Publisher 使用租约或批量读取发布到 Celery。
3. Worker 执行任务并写入结果或业务事件。
4. 失败按指数退避重试；超过上限进入死信。
5. idempotency_key 防止重复推理、通知和报表。

任务类型：image_quality、inference_pipeline、rule_evaluation、report_export、notification_create、overdue_scan、dead_letter_replay。

默认最多重试 3 次，退避 30 秒、2 分钟、10 分钟；不可重试错误包括格式不支持、租户越权、Schema 错误和无法标准化。死信重放必须由授权管理员发起并写审计。

## 5. AI 流水线

| 阶段 | 输入 | 输出 | 失败处理 |
|---|---|---|---|
| 质量检测 | 原图引用 | 清晰度、亮度、反光、遮挡、通过/补拍 | needs_retake |
| 目标检测 | 质量通过图片 | 试剂瓶、标签、相邻容器框 | 标记低覆盖 |
| OCR | 标签裁剪图 | 原文、字段候选、置信度 | 人工录入 |
| 实体标准化 | 字段候选 | 化学品实体候选、CAS/内部编号 | 待确认，不执行相容性 |
| 相邻关系 | 检测框和位置 | 可见相邻关系 | 关系不确定则无法判断 |
| 规则推理 | 实体、关系、规则版本 | 命中规则、严重度建议、解释 | 显示缺失/冲突 |

每个字段、实体和检测结果保存独立置信度。低于阈值或多个候选进入 needs_review；人工修改必须保留原值、新值、原因和操作人。首期解释由规则模板和结构化证据生成，不使用 LLM。

评测按第 1～3 天基线集、第 4～7 天临时门槛、第 8～10 天候选验收集、第 11～20 天报告执行；每类问题分别报告召回率、误报率、无法判断率、样本量和未覆盖场景。

## 6. 规则治理

规则最小结构包括 rule_id、version、scope、priority、when、then 和 explanation_template。when 支持危险类别、可见相邻关系和同柜条件；then 输出 finding_type、severity 和 action。

发布流程：草稿 -> 静态校验 -> 冲突检测 -> 专家审批 -> 回归测试 -> 发布/灰度 -> 监控 -> 回滚。

- 已发布版本不可原地修改。
- 同优先级冲突必须阻止发布。
- 冲突检测包含不同严重度、矛盾动作和不可达条件。
- 灰度按实验室启用；首期是否启用灰度仍待确认。
- 回滚只改变当前生效版本，不重写历史 Finding。
- 记录命中、确认、驳回、无法判断、误报和漏报指标。

## 7. 权限、隐私和租户隔离

| 角色 | 配置 | 采集 | AI复核 | 派发 | 复查销项 | 规则发布 | 原图导出 |
|---|---:|---:|---:|---:|---:|---:|---:|
| 安全管理员 | 全租户 | 是 | 是 | 是 | 是 | 审批/发布 | 按授权 |
| 实验室负责人 | 本实验室 | 查看 | 可确认 | 查看/建议 | 否 | 否 | 脱敏优先 |
| 安全员 | 授权实验室 | 是 | 是 | 否 | 是 | 否 | 按任务 |
| 整改责任人 | 无配置 | 否 | 否 | 否 | 否 | 否 | 仅任务证据 |
| 只读查看者 | 否 | 否 | 否 | 否 | 否 | 否 | 默认否 |

每个请求强制追加 tenant_id；实验室范围通过授权表校验；对象存储使用 tenant_id/laboratory_id/resource_id 前缀；导出再次校验租户和实验室范围。跨租户、路径穿越和批量接口测试是发布门禁。

原图默认校内保存；人员影像训练前打码或裁剪；留存期限由租户策略配置；删除采用软删除加对象清理任务，审计保留哈希、资源类型、操作者和时间。

## 8. Vue 前端与工作流

- Vue 3、TypeScript、Vite；路由使用 Vue Router。
- 服务端数据首选 TanStack Query for Vue；表单采用 Schema 驱动；状态机由 API 返回，前端不复制业务规则。
- 组件库、主题和状态管理具体选择仍待确认。
- 路由覆盖登录、总览、实验室、巡检采集、检查项复核、Finding、整改、规则、报表和审计。
- 采集状态：草稿、上传中、需补拍、处理中、待复核、已完成。
- Finding 状态：待复核、已确认、已驳回、无法判断、已派发。
- 整改状态：待派发、处理中、待复查、已销项、逾期、驳回、无法整改。
- 没有发现问题时显示“在本次图片和规则范围内未发现”，不能显示绝对安全。

## 9. 集成与通知

- 首期通知只写入 notifications，使用站内轮询或短轮询刷新。
- 外部消息使用 NotificationAdapter 接口预留，不实现邮件、企业微信或学校消息系统。
- MinIO 使用短期预签名 URL，浏览器不持有长期密钥。
- AI 服务只使用内网地址；健康检查和熔断由 Worker/客户端处理。

## 10. 需求追踪

| 需求 | 表/实体 | API/任务 | 测试 |
|---|---|---|---|
| PRD-004 模板采集 | inspection_templates、inspection_items | /inspections | TC-INS-001 |
| PRD-006 补拍 | asset_images、inference_runs | image_quality | TC-AI-001 |
| PRD-008 OCR | inference_runs | inference_pipeline | TC-AI-002 |
| PRD-009 标准化 | chemical_entities、chemical_aliases | entity_normalization | TC-AI-003 |
| PRD-010 相容性 | rule_versions、finding_rules | rule_evaluation | TC-RULE-001 |
| PRD-011 复核 | findings、audit_events | findings confirm | TC-WF-001 |
| PRD-013～017 整改闭环 | remediation_tasks、evidence | remediation API | TC-WF-002～004 |
| SEC-001 租户/RBAC | tenants、user_roles、scopes | 所有 API | TC-SEC-001～003 |
| SEC-005 审计 | audit_events | audit API | TC-SEC-004 |
| AI-009 规则版本 | rule_sets、rule_versions | rule API | TC-RULE-002～004 |
| NFR-002 性能 | inference_runs、指标 | Worker/HTTP | TC-PERF-001 |
| NFR-008 恢复 | 备份元数据、MinIO | 运维任务 | TC-DR-001 |

## 11. Gate 2 已确认的实现选择

1. Vue 组件库：Element Plus。
2. 服务端数据：TanStack Query for Vue；Pinia 仅管理会话和 UI 状态。
3. MySQL 迁移：SQLAlchemy 2 + Alembic + MySQL 8.0+。
4. Celery + Redis：Broker、Backend、缓存和锁使用独立 Redis DB；任务结果短期保留，业务事实写入 MySQL。
5. 规则首期不做自动百分比灰度，采用实验室级受控切换。
6. 评测集按类别记录实际样本量；基线目标为每类 20 个正样本和 20 个负样本，候选验收集目标为每类 50 个独立样本，样本不足如实标注。

