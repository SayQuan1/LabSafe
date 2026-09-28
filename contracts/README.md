# 机器可读设计契约

版本1.1.0；由 [build_specs.py](../tools/design/build_specs.py)生成；当前未声明已部署或已通过真实数据库迁移。

| 文件 | 内容 |
|---|---|
| [public-api-v1.yaml](public-api-v1.yaml) | 公共API请求、资源、响应、权限标签 |
| [inference-v1.yaml](inference-v1.yaml) | 独立AI内部协议 |
| [inference-routes-v1.json](inference-routes-v1.json) | 受控的租户/版本/设备到AI实例静态映射 |
| [operation-catalog.json](operation-catalog.json) | 全部公共操作索引 |
| [error-codes.json](error-codes.json) | 稳定错误和重试性 |
| [task-message-v1.json](task-message-v1.json)、[events-v1.json](events-v1.json) | 任务/事件判别联合 |
| [rule-dsl-v1.json](rule-dsl-v1.json)、[model-manifest-v1.json](model-manifest-v1.json) | 规则与制品结构 |
| [acceptance-policy-v1.json](acceptance-policy-v1.json)、[development-acceptance-policy.json](development-acceptance-policy.json) | 验收政策结构和明确标注proposed的开发目标 |
| [model-evaluation-v1.json](model-evaluation-v1.json)、[release-approvals-v1.json](release-approvals-v1.json) | 评测记录和部署方只读发布批准台账，不能由上传字段自我批准 |
| [data-model.json](data-model.json)、[database-design.sql](database-design.sql) | 列、键、约束、设计DDL |
| [examples/synthetic-rule-cases.json](examples/synthetic-rule-cases.json) | 模拟规则真值案例；非真实安全规则 |

修改生成源后重新生成并检查。Schema不替代领域guard、权限、租户关联、算法和发布门禁；语义见 [文档入口](../docs/README.md)。
