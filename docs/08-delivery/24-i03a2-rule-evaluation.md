# 08.24 I-03A2：规则评估任务执行

状态（2026-10-05）：接通 `facts_ready` 后的 `rule_evaluation` TaskDispatch、专属 lease/attempt/heartbeat/fencing、rules-dnf-v1 三值求值和原子 evaluation/findings 提交，并补齐固定快照语义与事实上下文投影。规则 bundle 只从当前 run 固定的已发布成员装载；本批使用合成 DSL fixture 验证工程逻辑，不代表真实化学安全规则已批准。

## 1. 本批交付

- `packages/rules/evaluator.py` 校验 RuleBundle DSL，按作用域/有效时间选择规则，按 priority/rule_id 稳定排序，支持 `eq`、`in`、`before_reference_date` 和 true/false/unknown 三值传播；严格检查原子类型、case 去重、解释占位符及配对守卫。
- `packages/persistence/rule_execution.py` 按 item→fact/evaluation→task 锁序领取规则任务，从 bundle 成员固定装载 published rule_versions、evaluator/checksum 快照，组装 expiry/storage/location/adjacency/subject 上下文，生成 needs_review findings，写入 evaluation result/hash/evidence，并收敛 run/item。
- `packages/application/rule_execution.py` 在事务外执行纯规则计算，提交时重新锁定并进行租约、generation、token 和当前指针围栏校验。
- `facts_ready` 结果现在同事务创建初始 `fact_revisions`、排队 `rule_evaluations`、`rule_evaluation` task 和 TaskDispatch outbox。
- durable dispatch 支持 `rule_evaluation` 消息；Worker 和 sweeper 增加显式 `WORKER_RULE_EVALUATION_ENABLED=1` 分支。

规则永久错误（DSL 无效、容量超限）不会自动重试；技术失败沿用专属 task retry/dead-letter。旧 generation、旧 token、旧 run、过期 lease 不写当前评估或 findings。零 finding 仍进入人工复核，不能自动完成。

## 2. 验证

| 命令 | 结果 |
|---|---|
| `.venv-business\\Scripts\\python.exe -m pytest tests/business/test_rule_evaluator.py tests/business/test_dispatch.py -q` | 37 passed |
| `PYTEST_ADDOPTS='-k inference_execution' .venv-business\\Scripts\\python.exe tools/database/run_mysql_tests.py --mysqld <mysqld>` | 1 passed；MySQL 8.0.33，43 表/115 外键，含 facts→rules→findings 闭环 |
| `.venv-business\\Scripts\\python.exe -m ruff check ...` | PASS |
| `.venv-business\\Scripts\\python.exe -m pytest tests/business tests/domain tests/protocol -q` | 1451 passed；1 个既有 Starlette/AnyIO 弃用警告 |
| `tools/design/build_specs.py --check` / `test_readiness_design.py` / `validate_specs.py` | 全部 PASS；148 synthetic readiness checks |

真实安全规则审批、规则管理 API、report/export、真实生产发布和完整 remediation 闭环仍未完成。
