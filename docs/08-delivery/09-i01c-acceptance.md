# I-01C：会话、RBAC、幂等与租户事务验收

状态：实现分支已完成本地验证，等待独立协作者审查；不得据此直接合并或部署。

## 实现边界

- `packages/domain/security.py`：纯 RBAC、实验室作用域、assignee/recheck/rule-approve 约束和 `expected_version` 乐观锁。
- `packages/persistence/security.py`：Argon2id 登录、数据库只存 token/CSRF 哈希、8 小时绝对会话、30 分钟 idle、session epoch、logout、审计、30 秒幂等租约、过期接管、响应重放和 lease-owner fencing。
- `packages/persistence/transaction.py`：显式事务要求、UTC 毫秒时间和租户行锁。
- `UserSecurityService`：禁用用户/撤销 `safety_admin` 时锁租户、锁用户、保护最后一名 active `safety_admin`，递增 `session_epoch`、撤销全部会话并在同一事务追加审计。

路由、Origin/JSON 校验、Redis 限流、完整用户角色授予 API、业务仓储和 I-02 状态机不在本 PR 内；路由只能通过 application service 调用上述原语，不能复制 SQL 或授权逻辑。

## 关闭证据

| 项目 | 命令 | 结果 |
|---|---|---|
| Python 3.11 语法、Ruff | `python -m py_compile ...`; `python -m ruff check apps packages tests tools/database`; `python -m ruff format --check apps packages tests tools/database` | 通过 |
| I-01C 纯安全测试 | `python -m pytest tests/security -q` | 7 passed |
| 原有业务/协议/AI 回归 | 三组 `unittest discover` | 6 + 4 + 15 passed |
| 隔离 MySQL | `tools/database/run_mysql_tests.py --mysqld <mysqld>` | 50 passed；MySQL 8.0.33；43 tables/115 FKs 无漂移 |
| 设计规格 | `build_specs.py --check`; `test_readiness_design.py` | 16 artifacts、148 checks 通过 |

MySQL 命令只创建本次运行的临时 data directory、随机 loopback 端口和 `labsafe_test_<uuid>` schema，结束后关闭并删除该实例；没有连接用户已有 MySQL。

## 独立审查前置项

1. 审查者核对 `main`/I-01B 基线后再审查本分支；确认权限、安全和数据库变更。
2. 补充 API 层的固定 cookie（`labsafe_session`、Secure、HttpOnly、SameSite=Lax、Path=/）、Origin、CSRF、限流和稳定错误映射。
3. 将所有写命令接入同一 unit-of-work：幂等行 → 业务聚合锁 → 业务写入 → 审计/Outbox → 幂等响应，统一提交或回滚。
4. 完成角色授予/撤销和 `/me`、`/auth/login`、`/auth/logout` 路由契约测试；撤权前再次授权，重放响应前再次授权。
5. 安装 `tools/design/requirements.txt` 中的 `openapi-spec-validator==0.7.2` 后执行 `validate_specs.py`；缺依赖不能算契约验证通过。

## 回滚

本 PR 不新增迁移。回滚为回退本特性分支提交；若后续 application 层已调用这些函数，先停止新路由并保留审计记录，再回退代码。不得删除会话或审计历史。
