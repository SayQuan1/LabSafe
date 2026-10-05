# 08.25 I-02：规则配置管理

状态（2026-10-05）：在规则执行快照基础上接通开发/测试环境规则配置 API。范围覆盖规则集创建与列表、规则版本导入、提交审批、批准、发布、退休、固定 bundle 创建与列表；所有写命令使用租户事务、幂等键、乐观版本、审计和发布状态守卫。

本批不批准任何真实化学安全规则，也不开放 production。规则 DSL 仍由 `rules-dnf-v1` evaluator 和 `contracts/rule-dsl-v1.json` 校验；bundle 只接受同租户 `published` rule_versions，按版本 ID 排序计算 checksum，成员创建后不可变。审批要求 `rule_expert`，且提交者不能批准自己的版本；旧 run 继续使用已固定的 bundle。

## 1. API

| 方法 | 路径 | 事务语义 |
|---|---|---|
| POST/GET | `/rule-sets` | safety_admin 创建；租户可见列表 |
| GET/POST | `/rule-versions` | published 版本列表；admin 导入 draft |
| GET | `/rule-versions/{id}` | 当前租户可见投影，草稿仅 admin/rule_expert |
| POST | `/rule-versions/{id}/submit-approval` | draft → submitted |
| POST | `/rule-versions/{id}/approve` | submitted → approved，审批人需 rule_expert 且不同于提交人 |
| POST | `/rule-versions/{id}/publish` | approved → published |
| POST | `/rule-versions/{id}/retire` | published → retired，仅影响新 bundle |
| GET/POST | `/rule-bundles` | 固定 published 成员；重复 checksum 幂等复用 |

`packages/persistence/rules.py` 负责行锁、状态转移、checksum、成员投影和审计；`packages/application/rules.py` 负责身份、租户锁、幂等响应和死锁重试；`apps/api/app/rules.py` 只做公共输入/路由适配。

## 2. 验证

| 命令 | 结果 |
|---|---|
| `.venv-business\\Scripts\\python.exe -m pytest tests/business -q` | 393 passed；1 个既有 Starlette/AnyIO 弃用警告 |
| `.venv-business\\Scripts\\python.exe -m ruff check ...` | PASS |
| `.venv-business\\Scripts\\python.exe -m ruff format --check ...` | PASS |
| `tools/design/build_specs.py --check` / `test_readiness_design.py` / `validate_specs.py` | PASS |

隔离 MySQL 的规则管理专项仍需补充；当前环境未提供本批专用 `mysqld` 路径，因此未把跳过的集成测试记为通过。人工事实修订和 finding 决策已在 [I-02 事实修订与人工复核](26-i02-review.md) 接通；remediation、report/export 和非图像任务 replay 仍属后续批次。
