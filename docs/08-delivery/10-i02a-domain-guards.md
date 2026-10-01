# I-02A 验收：领域命令守卫

日期：2026-09-30。基线：已合并的 I-01B PR #7、I-01C PR #8（`7fe593f`）。
状态：本批领域守卫已通过本地验证；整个 I-02 尚未完成。未提交、未推送，未取得本分支远程 CI 或独立评审结论。

## 1. 范围和实现入口

本批按[领域状态机](../02-architecture/02-domain-model.md)与[公共契约](../../contracts/public-api-v1.yaml)实现 13 个命令守卫，不重新定义状态机、权限或 API。入口是 [workflow.py](../../packages/domain/workflow.py)。

| 公共 operationId | 领域函数 | 判定与边界 |
|---|---|---|
| submitInspectionItem | submit_item | capture + 创建者/admin；开放巡检；1–3 张唯一 ready、可用、同 owner/location 图；首图 overview，detail 指向首图；当前 confirmed/dispatched/closed 均阻止重提 |
| retryInspectionItem | retry_item | review；item failed、当前 run failed；原图可用；返回原 pinned inputs 和 replay_of，不接受新模型/新规则参数 |
| completeInspectionItem | complete_item | review；当前评估 completed；无 needs_review finding；服务端 U/C 决定三种互斥结论 |
| confirmFinding / rejectFinding / cannotdetermineFinding | confirm_finding / reject_finding / cannot_determine_finding | 仅 current needs_review；reason 必填；确认要求可用证据；无法判断要求契约内 reason_code |
| dispatchRemediation | dispatch_finding | dispatch；current confirmed；同租户同实验室有效 remediator、未来期限；返回 finding dispatched + task pending_dispatch |
| acceptRemediation / submitevidenceRemediation | accept_task / submit_evidence | 实际 assignee；正确前置状态；提交 1–10 张唯一 ready 且归属当前 task 的可用图片 |
| recheckRemediation / rejectRemediation | recheck_task / reject_task | pending_recheck；请求、加载证据与 latest_evidence_id 三者一致；复查者不是 assignee 或证据提交人，包括 admin |
| cannotremediateRemediation / reassignRemediation | cannot_remediate / reassign_task | admin / dispatch；限定前置状态；原因及受派人/期限守卫；finding 保持 dispatched，不伪装销项 |

各命令都调用现有 `authorize`、`require_version`，不接收 `is_admin/is_reviewer/is_assignee` 权限开关，也不提供通用 status setter。不可见资源为 404，权限不足 403，输入非法 422，状态或版本冲突 409。

`RetryDecision` 保留原模型、词典、规则 bundle、pipeline、设备、reference_date 及按序的图片角色/父图清单；`RemediationDecision` 同时携带 task/finding 的目标状态。它们只是不可变判定结果，不代表数据库已提交。

## 2. 应用层接入要求

1. 从有效会话加载 `Principal`，在带 tenant/laboratory 条件的查询中加载资源，禁止将请求体直接构造成可信快照。角色变更和禁用仍必须在事务边界重新验证。
2. 按既有幂等与锁序加载 `Item/Finding/Task` 和关联对象。`current_findings` 必须是服务端完整集合，不能用分页、前端选中集合或缓存代替；评估和 findings 必须对应当前 run/evaluation。`Item.creator_id` 是所属 Inspection 创建者。
3. `Image.available`、`Finding.evidence_ready`、`FailedRun.configuration_available`、`assignee_active` 都是服务端校验结果，不是用户可提交字段。图片可用性需对应已保存的不可变对象版本/哈希；重试不得只确认某个同名 key 存在。原有 image/run_images 版本与哈希仍由应用层核对并复制。
4. 调用相应守卫。版本不匹配时返回冲突，不能自动补入新版本重试。传入 timezone-aware 的服务端 `now`，领域函数不读取系统时间。
5. 应用事务按命令完整写集落库：创建 run/task/evidence、更新 current 指针与版本、保留旧证据、审计、Outbox、幂等响应必须一起提交或回滚。领域判定函数本身不生成 ID、不递增版本、不写库。
6. guard 的放行不是绕过锁、授权或对象校验的凭据。查询投影、allowed_actions 与 HTTP schema 校验需要在下一批接入同一组守卫，不复制分支逻辑。

## 3. 验证与证据级别

Python 3.11；业务与 AI 使用原有独立环境。测试均为 synthetic 数据，不使用真实实验室图像、模型或业务库。

| 检查 | 可复现命令 | 本地结果 |
|---|---|---|
| 领域与授权 | `python -m pytest tests/domain tests/security -q` | 549 passed，其中 542 个领域用例、7 个原有安全用例 |
| 持久化单元回归 | 在上一条 pytest 命令追加 `tests/persistence/test_unit.py` | 共 570 passed；新增回归 21 个原有单元用例，不连接 MySQL |
| 静态检查 | `python -m ruff check apps packages tests tools/database`；`python -m ruff format --check apps packages tests tools/database` | 全部通过；61 个 Python 文件格式检查通过 |
| 业务/协议回归 | `python -m unittest discover -s tests/business -t . -v`；协议目录同命令 | 6 + 4 passed |
| AI 独立环境回归 | AI 环境运行 `python -m unittest discover -s tests/ai -t . -v`；协议目录同命令 | 15 + 4 passed；协议用例在两个环境分别验证，不重复计为新增用例 |
| 生成与设计 | `python -B tools/design/build_specs.py --check`；`test_readiness_design.py`；`validate_specs.py` | 16 份生成物无漂移、148 项就绪检查、完整设计校验 PASS（含 2 份 OpenAPI、162 个文档链接） |

最终集中执行：2026-09-30，Python 3.11.4。设计完整校验时间为 06:49 UTC（14:49 Asia/Shanghai）。首次完整设计校验因业务环境缺少 `openapi_spec_validator` 未能运行，随后通过已有独立设计依赖目录提供工具依赖，已重跑成功；未安装新依赖、未修改 requirements、未刷新生成报告来掩盖漂移。

测试包含每个命令的全状态矩阵、权限/租户/实验室/版本拒绝、120 组三值组合、已废弃或旧 run/evaluation、图片容量/归属/选择、原配置重试、自复查、错误 evidence、未来期限、不可变输入和数据库/OpenAPI 枚举对齐。

CI 的 business job **已配置**执行同一领域/安全命令；本分支尚未推送，因此没有宣称远程 CI 已通过。未改迁移或持久化代码；本批没有重新运行真实 MySQL，不复用 I-01C 的数据库成绩冒充本批验证。

## 4. 尚未完成的 I-02 工作

以下为下一批实施清单，不是本批已交付功能：

- 应用 Unit of Work、完整聚合仓储和并发测试：同 key 并发无重复副作用、旧版本竞争、幂等 pending/lease 接管与 fencing、失败事务回滚。
- 登录/会话/用户与角色 HTTP 接入：Origin/CSRF、cookie、Redis 限流、稳定错误响应、撤权后重放拒绝。不能将 I-01C 原语直接当成完整安全 API。
- 巡检创建/完成/取消、上传 grant/complete 及对象验证、人工事实完整快照与重评估、业务查询和上述 13 命令的事务/API 适配。
- Outbox/inbox、Worker 租约/回收/重放及独立 AI 执行闭环按 I-03 实施；真实 D-FINE-N/CUDA 验证走模型主线。

因此，FLOW-02/04/05/06 等目前只有本批相应守卫的单测证据，不代表完整端到端验收、API-02/03 并发验收或 production 放行。

## 5. 协作与回退

在特性分支保留改动，不直接提交或推送 main，不自动合并。原 `01-implementation-plan.md` 的本地排版改动完全保留，不纳入本批交付。

本批无数据库迁移、无外部状态修改。未来提交后可按协作规范 revert 本批代码/测试/CI/文档；若后续应用已依赖这些命令，必须连同依赖适配协调回退，不删除业务、审计或历史证据。状态机与授权相关实现仍需独立协作者审查。
