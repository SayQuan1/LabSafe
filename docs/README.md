# LabSafe 文档入口

状态：2026-09-27 设计修订候选1.1.0。先读 [最新审计](08-delivery/03-design-audit.md)，不要把结构检查通过当作全系统可上线。

## 阅读顺序

1. [文档控制](00-document-control.md)：规范边界和变更要求。
2. [产品](01-requirements/01-product-requirements.md)与[范围](01-requirements/01-scope-and-acceptance.md)。
3. [编码入口](02-architecture/04-coding-baseline.md)→[状态机](02-architecture/02-domain-model.md)→[AI进程](02-architecture/03-ai-inference-process.md)→[持久任务](02-architecture/05-durable-jobs.md)→[数据库](02-architecture/06-data-persistence.md)。
4. [API](03-interfaces/01-api-contract.md)、[模型与规则](04-ai-rules/01-data-and-rules.md)、[前端](05-frontend/01-interaction-spec.md)、[安全](06-security/01-security-privacy.md)。
5. [测试](07-quality-operations/01-testing-evaluation.md)、[运维](07-quality-operations/02-deployment-operations.md)、[试点](07-quality-operations/03-pilot-runbook.md)。
6. [实施计划](08-delivery/01-implementation-plan.md)、[需求追踪](08-delivery/02-traceability-matrix.md)、[问题解决方案](08-delivery/04-remediation-plan.md)。

## 机器可读设计

本轮已解决的设计决策入口：[模型和数据路线](04-ai-rules/02-model-data-plan.md)、[分层验收与校准](07-quality-operations/04-acceptance-policy.md)、[设计/实施/发布门禁](08-delivery/05-stage-gates.md)。从只有文档进入开发的具体任务见[实施计划](08-delivery/01-implementation-plan.md)；未执行的训练/迁移/试点不冒充已完成，也不再误列为设计无方案。

公共API、AI API、消息、规则/模型Schema、数据字典和DDL位于 [contracts](../contracts/README.md)。生成和检查入口见 [设计工具](../tools/design/README.md)。字段、自然语言守卫和测试必须一致，不能用某文件“优先”来隐藏冲突。

## 历史记录

[90-archive](90-archive/README.md)保留原Gate和旧文档，仅作追溯。现行主题文档不能依赖归档补充编码必要信息。
