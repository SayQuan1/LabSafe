# 04 领域模型与数据库设计

状态：**待用户确认**

## 1. 核心实体

| 实体 | 作用 |
|---|---|
| User | 用户、角色和所属范围 |
| Laboratory | 实验室及责任人 |
| Location | 区域、房间、货架、柜号和父子关系 |
| InspectionTemplate | 检查模板和检查项 |
| Inspection | 一次巡检任务 |
| InspectionItem | 模板中的一次采集项 |
| AssetImage | 原图、裁剪图和对象存储元数据 |
| InferenceRun | 一次模型推理及版本信息 |
| Finding | 一个可独立复核、派发和销项的问题 |
| ChemicalEntity | 标准化化学品实体和别名 |
| SafetyRule | 专家批准的规则及版本 |
| RemediationTask | 整改任务和状态机 |
| ReviewAction | 人工确认、修改、驳回等操作 |
| AuditEvent | 不可变审计事件 |
| ReportExport | 报表导出任务和下载信息 |

## 2. 关键字段

### Finding

`id`, `inspection_item_id`, `type`, `severity`, `status`, `evidence_image_ids`, `location_id`, `detected_facts_json`, `triggered_rule_ids`, `confidence`, `uncertainty_reason`, `requires_review`, `confirmed_by`, `confirmed_at`, `created_at`。

`status` 至少包括 `ai_detected`、`needs_review`、`confirmed`、`rejected`、`cannot_determine`、`dispatched`、`closed`。

### RemediationTask

`id`, `finding_id`, `assignee_id`, `created_by`, `due_at`, `status`, `description`, `priority`, `evidence_image_ids`, `reviewed_by`, `reviewed_at`, `rejection_reason`, `closed_at`。

状态：`pending_dispatch`、`in_progress`、`pending_recheck`、`closed`、`overdue`、`rejected`、`cannot_remediate`。

### InferenceRun

`id`, `inspection_item_id`, `model_version`, `rule_version`, `pipeline_version`, `input_image_ids`, `quality_result`, `detections_json`, `ocr_fields_json`, `normalized_entities_json`, `rule_results_json`, `started_at`, `finished_at`, `status`, `error_code`。

## 3. 关系与约束

- 一个实验室拥有多个 Location、Inspection 和 User 授权关系。
- 一个 Inspection 拥有多个 InspectionItem；每个 InspectionItem 可有多张 AssetImage 和多个 InferenceRun，但只有一个当前业务结果引用。
- 一个 Finding 最多关联一个主 RemediationTask，但可拥有多个 ReviewAction 和审计事件。
- 规则和模型均不可原地更新；发布产生新版本，历史记录只引用旧版本。
- 删除图片必须经过数据保留策略检查；业务记录保留不可逆审计摘要。

## 4. 索引与一致性

- `inspection(laboratory_id, created_at)`。
- `finding(status, severity, laboratory_id)`。
- `remediation_task(status, due_at, assignee_id)`。
- `inference_run(inspection_item_id, created_at)`。
- 所有状态转移使用事务，并写入 AuditEvent。
- JSON 字段只保存模型/规则原始证据；需要筛选的字段应结构化列化。
