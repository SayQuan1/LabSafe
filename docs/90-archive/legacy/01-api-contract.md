# 05 API与异步任务契约

状态：**待用户确认**

## 1. 通用约定

- REST JSON API，路径前缀 `/api/v1`。
- 所有响应带 `request_id`；错误格式为 `{code, message, details, request_id}`。
- 时间统一 ISO 8601 UTC 存储，前端按用户时区展示。
- 写操作要求幂等键 `Idempotency-Key`。
- 所有资源按权限过滤，禁止依赖前端隐藏按钮实现权限控制。

## 2. 认证与基础资源

```text
POST   /auth/login
GET    /me
GET    /laboratories
POST   /laboratories
GET    /laboratories/{id}
GET    /laboratories/{id}/locations
POST   /laboratories/{id}/locations
GET    /templates
POST   /templates
```

## 3. 巡检与上传

```text
POST   /inspections
GET    /inspections/{id}
POST   /inspections/{id}/items/{item_id}/upload-url
POST   /inspections/{id}/items/{item_id}/images
GET    /inspection-items/{id}
POST   /inspection-items/{id}/submit
POST   /inspection-items/{id}/retry
```

图片提交至少包含 `laboratory_id`、`location_id`、`template_item_id`、`captured_at`、`client_filename` 和对象存储键。

## 4. 推理与复核

```text
GET    /inspection-items/{id}/inference
GET    /findings?laboratory_id=&status=&severity=
GET    /findings/{id}
POST   /findings/{id}/confirm
POST   /findings/{id}/edit
POST   /findings/{id}/reject
POST   /findings/{id}/cannot-determine
```

确认请求必须包含操作者、确认意见和必要的字段修正；无法判断必须包含原因枚举。

## 5. 整改与复查

```text
POST   /findings/{id}/remediation-tasks
GET    /remediation-tasks
GET    /remediation-tasks/{id}
POST   /remediation-tasks/{id}/accept
POST   /remediation-tasks/{id}/submit-evidence
POST   /remediation-tasks/{id}/recheck
POST   /remediation-tasks/{id}/reject
POST   /remediation-tasks/{id}/cannot-remediate
```

状态变化只能通过领域命令接口发生，禁止通用 `PATCH status`。

## 6. 规则、模型和报表

```text
GET    /chemical-entities/search?q=
GET    /rules?status=published
POST   /rules/{id}/approve
GET    /model-versions
POST   /reports/exports
GET    /reports/exports/{id}
GET    /audit-events?resource_type=&resource_id=
```

## 7. 异步任务契约

任务状态：`queued`、`running`、`succeeded`、`failed`、`retrying`、`cancelled`。

任务消息必须含 `task_id`、`task_type`、`resource_id`、`idempotency_key`、`attempt`、`model_version`、`rule_version` 和 `created_at`。失败必须写入稳定错误码，不得只保存自由文本。
