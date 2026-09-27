# 08.2 需求到实现和验收追踪

用例定义均在 [测试目录](../07-quality-operations/01-testing-evaluation.md)。标识只引用已定义用例，不把未实现测试记为通过。

| ID / 需求 | 设计入口 | 主要数据 / 命令 | 验收案例 |
|---|---|---|---|
| R-01 组织与身份 | 安全、API | users/user_roles/sessions；login/grant/revoke | API-01、SEC-01、SEC-02 |
| R-02 巡检采集补拍 | 领域、前端、数据约束 | inspections/items/uploads/images；submit/retry | UP-01、FLOW-01、UI-01 |
| R-03 独立AI事实 | AI进程、D-FINE模型适配 | runs/run_images/derivatives；quality/runs | AI-01、AI-02、AI-03、AI-04、AI-05、AI-06、AI-07、AI-08、AI-09、UP-02 |
| R-04 规则与人工复核 | 领域、规则 | fact_revisions/evaluations/findings；facts/decisions/complete | RULE-01、RULE-02、FLOW-02、FLOW-03 |
| R-05 整改闭环 | 领域、前端 | remediation/evidence；dispatch/accept/recheck | FLOW-04、FLOW-05 |
| R-06 历史和并发 | 数据约束、持久任务 | pinned versions/current pointers/leases | API-02、API-03、DB-02、JOB-02 |
| R-07 故障恢复 | 持久任务、部署 | outbox/inbox/task_runs/attempts | JOB-01、JOB-03、OPS-01 |
| R-08 导出通知审计 | API、安全 | report snapshots/notifications/audit | EXPORT-01、AUDIT-01、SEC-01 |
| R-09 模型规则治理 | 模型规则、领域 | manifest/runtime-lock/dictionary/bundle/activation | MODEL-01、MODEL-02、RULE-02、EVAL-01 |
| R-10 本地化部署和试点 | 部署、试点 | 独立镜像、CUDA profile、离线制品、备份版本清单 | DB-01、OPS-01、OPS-02、AI-08、EVAL-01 |

| IRR关闭项 | 主需求 | 新增应用验收（待实现） |
|---|---|---|
| IRR-01重试入口 | R-06、R-07 | JOB-04 |
| IRR-02三值与完成 | R-04、R-05 | FLOW-06 |
| IRR-03容量闭包 | R-02、R-03、R-04 | CAP-01 |
| IRR-04对象权限 | R-01、R-02、R-08 | SEC-03 |
| IRR-05签名可达 | R-02、R-10 | UP-03 |
| IRR-06确定性事实 | R-03、R-04、R-09 | AI-10 |

每个R项同时具有权限拒绝测试，不能仅正向演示。实现PR标记对应R和案例ID；自动化覆盖报告与人工试点证据分别保存。
