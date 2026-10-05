# 08.23 I-03A2：推理任务专属执行

状态（2026-10-04）：在既有 I-03A1/A2 调度基础上接入 `inference_pipeline` 的领域租约、attempt、心跳、围栏结果提交和过期回收。worker 通过独立 AI HTTP 协议调用 `/internal/inference/v1/quality` 与 `/runs`；默认关闭，仅 `APP_ENV=dev/test` 可用。真实模型、GPU、生产路由和规则评估仍未完成。

## 1. 本批交付

- `packages/domain/inference_execution.py` 定义固定输入、attempt/fencing lease、request hash、结果 Schema/租户/版本/输入 hash 校验及可重试错误白名单。
- `packages/persistence/inference_execution.py` 按 item → current run → task 锁序领取 `inference_pipeline`，推进 `quality_checking/processing`，围栏提交 `needs_retake/needs_review`，技术失败进入 `retry_wait/dead_letter`，并支持过期租约回收。
- `packages/application/inference_execution.py` 提供独立心跳和可注入 AI client；同一不可变请求先做 quality，再做 runs 复验，quality 结果不一致按 `SCHEMA_MISMATCH` 收敛。
- `apps/worker/inference_pipeline.py` 与 worker 开关 `WORKER_INFERENCE_ENABLED=1` 接入 `q.general`；sweeper 同时回收 image/inference 执行租约。

AI 只返回事实，不直接写业务库。提交前必须同时匹配当前 run、submission revision、attempt id、fencing token、model/dictionary 版本及 SHA、pipeline 和有序图片 hash；旧 lease 或旧 run 结果被拒绝。`facts_ready` 会在同一围栏事务写入首个 `fact_revisions` 并把 run 推进到 `processing/facts`；规则评估任务和 `needs_review` 人工闭环仍属后续批次。

## 2. 验证

| 命令 | 结果 |
|---|---|
| `.venv-business\\Scripts\\python.exe -m pytest tests/business/test_inference_execution.py tests/business/test_dispatch.py tests/business/test_job_execution.py -q` | 56 passed |
| `.venv-business\\Scripts\\python.exe -m pytest tests/business tests/domain tests/protocol -q` | 1442 passed，1 个既有 Starlette/AnyIO 弃用警告 |
| `PYTEST_ADDOPTS='-k inference_execution' .venv-business\\Scripts\\python.exe tools/database/run_mysql_tests.py --mysqld <mysqld>` | 1 passed；MySQL 8.0.33，43 表/115 外键，head=0001_initial |
| `.venv-business\\Scripts\\python.exe -m ruff check ...` | PASS |
| `.venv-business\\Scripts\\python.exe -m compileall ...` | PASS |

本批尚未宣称真实 AI、MinIO/IAM/HTTPS、CUDA、facts/rules/report 完整闭环或 production 可用；worker consumer 仍需显式启用。
