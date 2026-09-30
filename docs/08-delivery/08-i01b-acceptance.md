# 08.8 I-01B 数据库基础验收记录

日期：2026-09-29。范围：初始迁移、租户/角色/初始管理员 CLI、真实 MySQL 结构与约束核对。
本机验证通过不等于协作者批准、远程 CI 通过或生产可用。

## 1. 分支与前置依赖

本地分支 wsq/i-01b-database-foundation，基于已合并的 I-01A PR #5（main 提交 3a92e09）。
I-01B PR #7 已推送并以 main 为目标；本页保留分支依赖关系，数据库/安全变更仍需两名协作者共同确认。
远程 CI 已运行并通过 business-python311、ai-python311、design-contracts、persistence-mysql 和 web 检查；通过 CI 不替代代码评审。

## 2. 已交付

| 交付项 | 代码入口与约束 |
|---|---|
| 初始 Alembic 迁移 | packages/persistence/migrations/versions/0001_initial.py；43 张业务表、115 个 FK，另有 Alembic 版本表 |
| 不可变迁移输入 | migrations/snapshots/0001_initial.sql/json；执行时不读取可变的 contracts；wheel 包含快照和 revision |
| 连接保护 | database.py；Python 3.11、显式 dev/test、同环境前缀 schema、秘密文件、回环地址、MySQL 8.x 且不低于 8.0.16 |
| 迁移保护 | 非空初始库/视图和 offline 模式拒绝；迁移持有 schema 级命名锁；downgrade 仅允许显式确认的可丢弃 test 库 |
| 初始化 | cli.py / bootstrap.py；租户、固定六角色、租户级 safety_admin、脱敏审计同事务提交；并发初始化串行化，重复租户不重置密码 |
| 密码与账号 | 交互 getpass 或秘密文件；密码 12–128 字符，不 trim/规范化；Argon2id m=65536 KiB、t=3、p=1、salt=16 bytes、hash=32 bytes；用户名 NFKC+casefold |
| 结构核对 | schema.py；表/列顺序、类型、nullability、默认值、生成列、PK/unique/index/FK/check、collation、UTC/strict/FK/unique session 设置 |
| 测试与 CI | tests/persistence、tools/database/run_mysql_tests.py；新增独立 MySQL CI job，不给 AI 安装数据库或密码散列依赖 |

结构比较只规范化 MySQL 的表示差异，不忽略 NOT NULL、CHECK 优先级或 ENUM 字符串的大小写差异。
六角色是 safety_admin、lab_manager、inspector、remediator、viewer、rule_expert；只为初始管理员创建 safety_admin 授权，不自动授予其他角色。
初始化 CLI 不是登录、会话或密码重置实现。

## 3. 本机实际验证

Windows、Python 3.11.4、MySQL Community Server 8.0.33。数据库测试使用新的临时目录、随机回环端口、随机凭据与专用随机 schema。
启动器使用 --no-defaults，并在任何写操作前核对该实例的 datadir/port；没有使用、注册、修改或停止已有 MySQL 服务。
测试结束已删除本次临时 schema/data directory 并关闭本次子进程。

| 检查 | 实际结果 |
|---|---|
| I-01B 测试套件 | 47 项通过：26 项真实 MySQL 用例 + 21 项配置/安全/规范化/快照单元用例 |
| 迁移周期 | upgrade → 核对 → downgrade → re-upgrade → 再核对；43 表、115 FK、head=0001_initial、差异为空 |
| 保护反例 | 非空库 sentinel 保留；未确认 downgrade 拒绝且原结构不变；生产/远端连接/offline/错误 schema/缺密码/额外 URL 参数拒绝 |
| 数据约束 | 跨 tenant、错 item current 指针、重复 scope、owner 同空/同非空、重复 crop、错 run/parent/evidence、非法 attempt/state、受引用父项删除均拒绝；合法路径通过 |
| 拒绝错误码 | FK=1452；父记录 RESTRICT=1451；唯一键=1062；CHECK=3819；严格模式非法 ENUM=1265 |
| 结构校验反例 | 实际修改列长度、默认值、唯一索引、FK、CHECK enforcement、额外索引六类漂移均被发现，恢复后再次无差异 |
| 初始化事务 | 注入 audit 写入失败后所有记录回滚；并发同租户只生成一个管理员；重复调用不改变密码；密码/hash 不进入输出或审计 changes |
| I-01A 回归 | 6 项业务 + 4 项协议 + 15 项 AI 测试通过；协议在独立 AI 环境另行复验通过，不重复计数 |
| 环境与静态检查 | 两环境 pip check 通过；AI 无 SQLAlchemy/Alembic/PyMySQL/Argon2/Celery/Redis/模型依赖；Ruff check/format 通过（50 个 Python 文件） |
| 发布包 | 使用声明的隔离构建依赖构建 wheel，安装到新临时目录后可找到两份快照和 Alembic head，不依赖源码目录 |
| 设计检查 | build_specs --check、148 项 IRR 合成检查、validate_specs（含 41 发布/39 D-FINE 合成检查）通过 |
| CI 配置 | YAML 结构校验通过；PR #7 的新 MySQL job 已在远程运行并通过，未复用 PR #5 的成绩 |

MySQL 8.0.33 只是本次本机/CI 兼容性测试基线，不是生产镜像或安全补丁选型结论。
设计工具仍声明未运行应用/数据库测试，是该工具自身的证据范围；本页单独记录真实数据库结果，二者不能互相替代。

## 4. 本轮发现并关闭的差异

真实 introspection 发现设计字典要求 user_roles.scope_key 非空，但 DDL 生成器遗漏 NOT NULL。
已修复 tools/design/data_model.py、重新生成 contracts/database-design.sql，并同步 0001_initial 快照；数据字典和角色规则没有变更。
MySQL 8 的默认认证所需 RSA 依赖通过 PyMySQL[rsa] 补齐；Argon2id 和时区依赖仅加入业务侧。

## 5. 操作、回滚与后续边界

完整环境准备、upgrade/verify/bootstrap-tenant、秘密文件、退出码、失败处理和命令见 [开发指南](../../DEVELOPMENT.md) 第 3.5 节。
正常代码回滚不授权删除业务库。首迁移 DDL 中途失败可能留下部分表；保留失败证据，仅在操作者确认的可丢弃库重建。
downgrade 删除 43 张业务表，必须 APP_ENV=test 且 LABSAFE_ALLOW_DESTRUCTIVE_DOWNGRADE 与 DATABASE_SCHEMA 完全一致；不是生产恢复方案。
历史迁移发布后不得就地修改快照；新增 revision 并维护对应 head 的核对基准。

- I-01C：业务仓储、session/RBAC、幂等、租户事务、最后管理员保护及角色/实验室授权守卫。
- I-02/I-03：跨资源/JSON 语义一致性、状态机、持久任务、旧租约 fencing 回写拒绝、Worker→AI→DB。
- I-ML/I-05/R：真实 D-FINE-N/CUDA/图片评测、远端 TLS 与生产账户最小权限、备份恢复、部署和人工批准。

以上后续事项未在 I-01B 实现，不能用 FK 检查、schema 通过、开发 fixture 或既有 CI 结果代替验收。
