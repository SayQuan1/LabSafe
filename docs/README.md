# LabSafe 文档入口

状态：设计版本 1.1.0；实现进度更新于 2026-10-03。设计结论见 [最新审计](08-delivery/03-design-audit.md)，不要把结构检查或局部实现通过当作全系统可上线。

## 阅读顺序

实现进度：I-01A/B/C、I-02A/B/C/D/E/F1/F2 已合并（后者为 PR #9）；当前在 I-03A1/A2 调度与围栏、I-02F3 general 图像处理、I-03A3 图像任务查询/管理员重放上接通 I-02F4 ready 图像受控下载，并已接通 I-02G1 `submitInspectionItem` 的 run/task/outbox 原子入队；代码本地完成、真实存储部署验收与推理 worker 仍待关闭，完整 I-02/I-03 仍未完成。审批状态、远程 CI 与本机测试分别记录。
运行入口见 [开发指南](../DEVELOPMENT.md)，本轮证据见 [I-02G1 提交入队验收](08-delivery/22-i02g1-submit-inference-enqueue.md)，前序证据见 [I-02F4 图片下载验收](08-delivery/21-i02f4-image-download.md) 与 [I-03A3 图像任务重放](08-delivery/20-i03a3-image-job-replay.md)，完整阶段状态见下方实施计划。

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
