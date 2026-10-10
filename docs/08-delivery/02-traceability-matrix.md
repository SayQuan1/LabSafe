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

2026-10-09 第41批本地覆盖R-03/R-06/R-09及AI-10/CAP-01候选部分：冻结开发词典/阈值、CAS精确/别名/有界模糊、全部同瓶名称共识/冲突与top5、Worker受控原始快照核验/完整重算、候选/context围栏保存。实际MinIO固定V1与D准确版本/SHA/复用、11项IAM拒绝补充SEC-03工程证据；nginx/公开下载签名/TLS/持久部署未验，不关闭SEC-03/UP-03整个门禁。用户四图与明确synthetic词典/框正向probe分别记录，仍needs_review，不关闭完整AI-10或现场/生产验收。见[第41批](41-i-ml01-chemical-candidates.md)，下一批第42批日期聚合；第36–41批未提交/Push/远程CI。

2026-10-09 第40批本地覆盖R-03/R-06及AI-10/CAP-01字段部分：ocr-fields-v1、严格日期/CAS、同瓶全局连续配对、全部source_lines、Worker完整重算及围栏保存。真实官方CPU/用户素材工程探针与明确synthetic框正向证据分别记录，不关闭实体/日期事实、现场评测或生产批准。见[第40批](40-i-ml01-ocr-fields.md)，下一批第41批冻结开发化学词典/候选；未提交/Push/远程CI。

2026-10-07 第36批覆盖R-03/R-06及AI-03、AI-04、DB-02、JOB-02的局部输入/版本/期限基础：冻结版本、受控GET、实算SHA、原Worker图片ID、超时回收与旧lease拒绝，见[受控analysis输入证据](36-i-ml01-controlled-analysis-input.md)。SEC-03/UP-03真实对象存储IAM/TLS、AI-10业务事实、常驻HTTP仍待验收，局部覆盖不关闭整个案例。

第37批本地覆盖R-03/R-06及AI-03/AI-04/DB-02/JOB-02/CAP-01的文字证据部分：正式独立区域/闭包、Worker源/PNG/识别摘要、准确D版本、围栏全量事务和004历史迁移。成绩及尚未覆盖的常驻HTTP/现场/生产范围见[交付](37-i-ml01-worker-ocr-evidence.md)，不关闭整个AI-10或模型接线任务。

2026-10-07 第38批本地覆盖R-03/R-06/R-07/R-09及AI-03/AI-04/JOB-02/CAP-01的常驻CPU工程部分：实际加载身份、ready/version、固定bundle/lock、正式RPC/Worker pin、有界socket、崩溃重载与退出清理。真实官方权重/合成图及S3模拟器证据、MySQL定向围栏分别记录；未实现完整公共activation/多版本路由、语义事实、现场/IAM/TLS或生产批准，不关闭整个真实模型接线任务或AI-10。见[第38批](38-i-ml01-resident-cpu-http.md)，下一批第39批关联/未知保留；未提交/Push/远程CI。

2026-10-09 第39批本地覆盖R-03/R-06及AI-10/CAP-01的文字/bottle关联部分：实际quad面积/中心/唯一最小框、NULL未知、Worker重算、稳定ID/像素/全量登记与公共投影。正向与并列用显式合成框/实际像素/HTTP/MySQL另测；官方权重合成标签HTTP为0瓶/6未知，不代替现场关联成绩。无字段/词典/日期或生产批准，不关闭整个真实模型接线任务。见[第39批](39-i-ml01-text-bottle-association.md)，下一批第40批字段/多行证据，未提交/Push/远程CI。
